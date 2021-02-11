#!/usr/bin/env python
# coding: utf-8

import datetime
import argparse
import glob
import re
import yaml
import copy
import pandas as pd

import numpy as np
import scipy
from scipy.sparse.csgraph import connected_components

import traitlets

import ctapipe

from ctapipe_io_magic import MAGICEventSource

from ctapipe.io import HDF5TableWriter
from ctapipe.core.container import Container, Field
from ctapipe.reco import HillasReconstructor
from ctapipe.image import hillas_parameters, leakage
from ctapipe.image.timing import timing_parameters
from ctapipe.instrument import CameraGeometry

from astropy import units as u
from astropy.coordinates import SkyCoord, AltAz

from utils import MAGIC_Badpixels
# from utils import bad_pixel_treatment
from utils import MAGIC_Cleaning
from utils.MAGIC_StereoCalc import stereo_par_calc_mars

def info_message(text, prefix='info'):
    """
    This function prints the specified text with the prefix of the current date

    Parameters
    ----------
    text: str

    Returns
    -------
    None

    """

    date_str = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    print(f"({prefix:s}) {date_str:s}: {text:s}")

def get_num_islands(camera, clean_mask, event_image):
    """Get the number of connected islands in a shower image.

    Parameters
    ----------
    camera : CameraGeometry
        Description
    clean_mask : np.array
        Cleaning mask
    event_image : np.array
        Event image

    Returns
    -------
    int
        Number of islands
    """

    neighbors = camera.neighbor_matrix_sparse
    clean_neighbors = neighbors[clean_mask][:, clean_mask]
    num_islands, labels = connected_components(clean_neighbors, directed=False)

    return num_islands

def scale_camera_geometry(camera, factor):
    """Scale given camera geometry of a given (constant) factor
    
    Parameters
    ----------
    camera : CameraGeometry
        Camera geometry
    factor : float
        Scale factor
    
    Returns
    -------
    CameraGeometry
        Scaled camera geometry
    """
    pix_x_scaled = factor*camera.pix_x
    pix_y_scaled = factor*camera.pix_y
    pix_area_scaled = geom.guess_pixel_area(pix_x_scaled, pix_y_scaled, camera.pix_type)

    return CameraGeometry(
        camera_name='MAGICCam',
        pix_id=camera.pix_id,
        pix_x=pix_x_scaled,
        pix_y=pix_y_scaled,
        pix_area=pix_area_scaled,
        pix_type=camera.pix_type,
        pix_rotation=camera.pix_rotation,
        cam_rotation=camera.cam_rotation
        )

def process_dataset_mc(input_mask, output_name):
    """Create event metadata container to hold event / observation / telescope
    IDs and MC true values for the event energy and direction. We will need it
    to add this information to the event Hillas parameters when dumping the
    results to disk.

    Parameters
    ----------
    input_mask : str
        Mask for MC input files. Reading of files is managed
        by the MAGICEventSource class.
    output_name : str
        Name of the HDF5 output file.

    Returns
    -------
    None
    """

    class InfoContainer(Container):
        obs_id = Field(-1, "Observation ID")
        event_id = Field(-1, "Event ID")
        tel_id = Field(-1, "Telescope ID")
        true_energy = Field(-1, "MC event energy", unit=u.TeV)
        true_alt = Field(-1, "MC event altitude", unit=u.rad)
        true_az = Field(-1, "MC event azimuth", unit=u.rad)
        true_core_x = Field(-1, "MC event x-core position", unit=u.m)
        true_core_y = Field(-1, "MC event y-core position", unit=u.m)
        tel_alt = Field(-1, "MC telescope altitude", unit=u.rad)
        tel_az = Field(-1, "MC telescope azimuth", unit=u.rad)
        n_islands = Field(-1, "Number of image islands")

    class ObsIdContainer(Container):
        obs_id = Field(-1, "Observation ID")


    cleaning_config = dict(
        picture_thresh = 6,
        boundary_thresh = 3.5,
        max_time_off = 4.5 * 1.64,
        max_time_diff = 1.5 * 1.64,
        usetime = True,
        usesum = True,
        findhotpixels = False,
    )

    aberration_factor = 1./1.0713

    # Now let's loop over the events and perform:
    #  - image cleaning;
    #  - hillas parameter calculation;
    #  - time gradient calculation.
    #  
    # We'll write the result to the HDF5 file that can be used for further processing.

    hillas_reconstructor = HillasReconstructor()

    horizon_frame = AltAz()

    # Opening the output file
    with HDF5TableWriter(filename=output_name, group_name='dl1', overwrite=True) as writer:
        # Event source
        source = MAGICEventSource(input_url=input_mask)
        
        camera = source.subarray.tel[1].camera
        camera_scaled = scale_camera_geometry(camera, aberration_factor)

        magic_clean = MAGIC_Cleaning.magic_clean(camera_scaled,cleaning_config)

        obs_id_last = -1

        # Looping over the events
        for event in source:
            
            if event.index.obs_id != obs_id_last:
                obs_id_info = ObsIdContainer(obs_id=event.index.obs_id)
                writer.write("mc_header", (obs_id_info, event.mcheader))
                obs_id_last = event.index.obs_id

            tels_with_data = event.r1.tels_with_data
            
            computed_hillas_params = dict()
            telescope_pointings = dict()
            array_pointing = SkyCoord(
                alt=event.pointing.array_altitude,
                az=event.pointing.array_azimuth,
                frame=horizon_frame,
            )

            # Looping over the triggered telescopes
            for tel_id in tels_with_data:
                # Obtained image
                event_image = event.dl1.tel[tel_id].image
                # Pixel arrival time map
                event_pulse_time = event.dl1.tel[tel_id].peak_time

                clean_mask, event_image, event_pulse_time = magic_clean.clean_image(event_image, event_pulse_time)

                num_islands = get_num_islands(camera_scaled, clean_mask, event_image)

                event_image_cleaned = event_image.copy()
                event_image_cleaned[~clean_mask] = 0

                event_pulse_time_cleaned = event_pulse_time.copy()
                event_pulse_time_cleaned[~clean_mask] = 0

                if np.any(event_image_cleaned):
                    try:
                        # If event has survived the cleaning, computing the Hillas parameters
                        hillas_params = hillas_parameters(camera_scaled, event_image_cleaned)
                        image_mask = event_image_cleaned > 0
                        timing_params = timing_parameters(
                            camera_scaled,
                            event_image_cleaned,
                            event_pulse_time_cleaned,
                            hillas_params,
                            image_mask
                        )
                        leakage_params = leakage(camera_scaled, event_image, clean_mask)

                        computed_hillas_params[tel_id] = hillas_params

                        telescope_pointings[tel_id] = SkyCoord(
                            alt=event.pointing.tel[tel_id].altitude,
                            az=event.pointing.tel[tel_id].azimuth,
                            frame=horizon_frame,
                        )

                        # Preparing metadata
                        event_info = InfoContainer(
                            obs_id=event.index.obs_id,
                            event_id=scipy.int32(event.index.event_id),
                            tel_id=tel_id,
                            true_energy=event.mc.energy,
                            true_alt=event.mc.alt.to(u.rad),
                            true_az=event.mc.az.to(u.rad),
                            true_core_x=event.mc.core_x.to(u.m),
                            true_core_y=event.mc.core_y.to(u.m),
                            tel_alt=event.pointing.tel[tel_id].altitude.to(u.rad),
                            tel_az=event.pointing.tel[tel_id].azimuth.to(u.rad),
                            n_islands=num_islands
                        )

                        # Storing the result
                        writer.write("hillas_params", (event_info, hillas_params, leakage_params, timing_params))

                    except ValueError:
                        print(f"Event ID {event.index.event_id} (obs ID: {event.index.obs_id}; "
                            f"telescope ID: {tel_id}): Hillas calculation failed.")
                else:
                    print(f"Event ID {event.index.event_id} (obs ID: {event.index.obs_id}; "
                        f"telescope ID: {tel_id}) did not pass cleaning.")

            if len(computed_hillas_params.keys()) > 1:
                if any([computed_hillas_params[tel_id]["width"].value == 0 for tel_id in computed_hillas_params]):
                    print(f"Event ID {event.index.event_id} (obs ID: {event.index.obs_id}) "
                        f"has an ellipse with width=0: stereo parameters calculation skipped.")
                elif any([np.isnan(computed_hillas_params[tel_id]["width"].value) for tel_id in computed_hillas_params]):
                    print(f"Event ID {event.index.event_id} (obs ID: {event.index.obs_id}) "
                        f"has an ellipse with width=NaN: stereo parameters calculation skipped.")
                else:
                    stereo_params_mars = stereo_par_calc_mars(computed_hillas_params, source.subarray, telescope_pointings)
                    stereo_params_cta = hillas_reconstructor.predict(computed_hillas_params, source.subarray, array_pointing)
                    event_info.tel_id = -1
                    # Storing the result
                    writer.write("stereo_params_mars", (event_info, stereo_params_mars))
                    writer.write("stereo_params_cta", (event_info, stereo_params_cta))


def process_dataset_data(input_mask, output_name):
    """Create event metadata container to hold event / observation / telescope
    IDs and MC true values for the event energy and direction. We will need it
    to add this information to the event Hillas parameters when dumping the
    results to disk.

    Parameters
    ----------
    input_mask : str
        Mask for real data input files. Reading of files is managed
        by the MAGICEventSource class.
    output_name : str
        Name of the HDF5 output file.

    Returns
    -------
    None
    """

    class InfoContainer(Container):
        obs_id = Field(-1, "Observation ID")
        event_id = Field(-1, "Event ID")
        tel_id = Field(-1, "Telescope ID")
        mjd = Field(-1, "Event MJD")
        tel_alt = Field(-1, "MC telescope altitude", unit=u.rad)
        tel_az = Field(-1, "MC telescope azimuth", unit=u.rad)
        n_islands = Field(-1, "Number of image islands")

    cleaning_config = dict(
        picture_thresh = 6,
        boundary_thresh = 3.5,
        max_time_off = 4.5 * 1.64,
        max_time_diff = 1.5 * 1.64,
        usetime = True,
        usesum = True,
        findhotpixels=True,
    )

    bad_pixels_config = dict(
        pedestalLevel = 400,
        pedestalLevelVariance = 4.5,
        pedestalType = 'FromExtractorRndm'
    )

    aberration_factor = 1./1.0713

    # Now let's loop over the events and perform:
    #  - image cleaning;
    #  - hillas parameter calculation;
    #  - time gradient calculation.
    #  
    # We'll write the result to the HDF5 file that can be used for further processing.

    hillas_reconstructor = HillasReconstructor()

    horizon_frame = AltAz()

    previous_event_id = 0

    # Opening the output file
    with HDF5TableWriter(filename=output_name, group_name='dl1', overwrite=True) as writer:
        # Creating an input source
        source = MAGICEventSource(input_url=input_mask)

        camera = source.subarray.tel[1].camera
        camera_scaled = scale_camera_geometry(camera, aberration_factor)
        magic_clean = MAGIC_Cleaning.magic_clean(camera_scaled,cleaning_config)
        badpixel_calculator = MAGIC_Badpixels.MAGICBadPixelsCalc(config=bad_pixels_config)

        # Looping over the events
        for event in source:
            #Exclude pedestal runs??
            #print(event.index.obs_id, event.index.event_id, event.meta['number_subrun'])
            if previous_event_id == event.index.event_id:
                continue
            previous_event_id = copy.copy(event.index.event_id)

            tels_with_data = event.r1.tels_with_data

            computed_hillas_params = dict()
            telescope_pointings = dict()
            array_pointing = SkyCoord(
                alt=event.pointing.array_altitude,
                az=event.pointing.array_azimuth,
                frame=horizon_frame,
            )

            # Looping over the triggered telescopes
            for tel_id in tels_with_data:
                # Obtained image
                event_image = event.dl1.tel[tel_id].image
                # Pixel arrival time map
                event_pulse_time = event.dl1.tel[tel_id].peak_time

                badrmspixel_mask = badpixel_calculator.get_badrmspixel_mask(event)
                deadpixel_mask = badpixel_calculator.get_deadpixel_mask(event)
                unsuitable_mask = np.logical_or(badrmspixel_mask[tel_id-1], deadpixel_mask[tel_id-1])

                clean_mask, event_image, event_pulse_time = magic_clean.clean_image(event_image, event_pulse_time,unsuitable_mask=unsuitable_mask)

                num_islands = get_num_islands(camera_scaled, clean_mask, event_image)

                event_image_cleaned = event_image.copy()
                event_image_cleaned[~clean_mask] = 0

                event_pulse_time_cleaned = event_pulse_time.copy()
                event_pulse_time_cleaned[~clean_mask] = 0

                if np.any(event_image_cleaned):
                    try:
                        # If event has survived the cleaning, computing the Hillas parameters
                        hillas_params = hillas_parameters(camera_scaled, event_image_cleaned)
                        image_mask = event_image_cleaned > 0
                        timing_params = timing_parameters(
                            camera_scaled,
                            event_image_cleaned,
                            event_pulse_time_cleaned,
                            hillas_params,
                            image_mask
                        )
                        leakage_params = leakage(camera_scaled, event_image, clean_mask)

                        computed_hillas_params[tel_id] = hillas_params

                        telescope_pointings[tel_id] = SkyCoord(
                            alt=event.pointing.tel[tel_id].altitude,
                            az=event.pointing.tel[tel_id].azimuth,
                            frame=horizon_frame,
                        )

                        # Preparing metadata
                        event_info = InfoContainer(
                            obs_id=event.index.obs_id,
                            event_id=scipy.int32(event.index.event_id),
                            tel_id=tel_id,
                            mjd=event.trigger.time.mjd,
                            tel_alt=event.pointing.tel[tel_id].altitude.to(u.rad),
                            tel_az=event.pointing.tel[tel_id].azimuth.to(u.rad),
                            n_islands=num_islands
                        )

                        # Storing the result
                        writer.write("hillas_params", (event_info, hillas_params, leakage_params, timing_params))

                    except ValueError:
                        print(f"Event ID {event.index.event_id} (obs ID: {event.index.obs_id}; "
                            f"telescope ID: {tel_id}): Hillas calculation failed.")
                else:
                    print(f"Event ID {event.index.event_id} (obs ID: {event.index.obs_id}; "
                        f"telescope ID: {tel_id}) did not pass cleaning.")

            if len(computed_hillas_params.keys()) > 1:
                if any([computed_hillas_params[tel_id]["width"].value == 0 for tel_id in computed_hillas_params]):
                    print(f"Event ID {event.index.event_id} (obs ID: {event.index.obs_id}) "
                        f"has an ellipse with width=0: stereo parameters calculation skipped.")
                elif any([np.isnan(computed_hillas_params[tel_id]["width"].value) for tel_id in computed_hillas_params]):
                    print(f"Event ID {event.index.event_id} (obs ID: {event.index.obs_id}) "
                        f"has an ellipse with width=NaN: stereo parameters calculation skipped.")
                else:
                    stereo_params_mars = stereo_par_calc_mars(computed_hillas_params, source.subarray, telescope_pointings)
                    stereo_params_cta = hillas_reconstructor.predict(computed_hillas_params, source.subarray, array_pointing)
                    event_info.tel_id = -1
                    # Storing the result
                    writer.write("stereo_params_mars", (event_info, stereo_params_mars))
                    writer.write("stereo_params_cta", (event_info, stereo_params_cta))

# =================
# === Main code ===
# =================

# --------------------------
# Adding the argument parser
arg_parser = argparse.ArgumentParser(description="""
This tools computes the Hillas parameters for the specified data sets.
""")

arg_parser.add_argument("--config", default="config.yaml",
                        help='Configuration file to steer the code execution.')
arg_parser.add_argument("--usereal",
                        help='Process only real data files.',
                        action='store_true')
arg_parser.add_argument("--usemc",
                        help='Process only simulated data files.',
                        action='store_true')
arg_parser.add_argument("--usetest",
                        help='Process only test files.',
                        action='store_true')
arg_parser.add_argument("--usetrain",
                        help='Process only train files.',
                        action='store_true')

parsed_args = arg_parser.parse_args()
# --------------------------

# ------------------------------
# Reading the configuration file

file_not_found_message = """
Error: can not load the configuration file {:s}.
Please check that the file exists and is of YAML or JSON format.
Exiting.
"""

try:
    config = yaml.safe_load(open(parsed_args.config, "r"))
except IOError:
    print(file_not_found_message.format(parsed_args.config))
    exit()

if 'data_files' not in config:
    print('Error: the configuration file is missing the "data_files" section. Exiting.')
    exit()
    
if 'image_cleaning' not in config:
    print('Error: the configuration file is missing the "image_cleaning" section. Exiting.')
    exit()
# ------------------------------

if parsed_args.usereal and parsed_args.usemc:
    data_type_to_process = config['data_files']
elif parsed_args.usereal:
    data_type_to_process = ['data']
elif parsed_args.usemc:
    data_type_to_process = ['mc']
else:
    data_type_to_process = config['data_files']

if parsed_args.usetrain and parsed_args.usetest:
    data_sample_to_process = ['train_sample', 'test_sample']
elif parsed_args.usetrain:
    data_sample_to_process = ['train_sample']
elif parsed_args.usetest:
    data_sample_to_process = ['test_sample']
else:
    data_sample_to_process = ['train_sample', 'test_sample']

telescopes_to_process = list(config['image_cleaning'].keys())

for data_type in data_type_to_process:
    for sample in data_sample_to_process:
        for telescope_type in telescopes_to_process:
            if telescope_type not in config['data_files'][data_type][sample]:
                raise ValueError(f'Telescope type "{telescope_type}" is not in the configuration file')

            if telescope_type not in config['image_cleaning']:
                raise ValueError(f'Telescope type "{telescope_type}" does not have image cleaning settings')

            info_message(f'Data "{data_type}", sample "{sample}", telescope "{telescope_type}"',
                prefix='Hillas')

            is_mc = data_type.lower() == "mc"

            if is_mc:
                process_dataset_mc(input_mask=config['data_files'][data_type][sample][telescope_type]['input_mask'],
                    output_name=config['data_files'][data_type][sample][telescope_type]['hillas_output'])
            else:
                process_dataset_data(input_mask=config['data_files'][data_type][sample][telescope_type]['input_mask'],
                    output_name=config['data_files'][data_type][sample][telescope_type]['hillas_output'])
