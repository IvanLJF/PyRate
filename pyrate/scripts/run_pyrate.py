#   This Python module is part of the PyRate software package.
#
#   Copyright 2017 Geoscience Australia
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
"""
This Python module runs the main PyRate processing workflow
"""
from __future__ import print_function

import logging
import os
from os.path import join
import pickle as cp
import numpy as np

from pyrate import algorithm
from pyrate import config as cf
from pyrate.config import ConfigException
from pyrate import ifgconstants as ifc
from pyrate import linrate
from pyrate import mpiops
from pyrate import mst
from pyrate import orbital
from pyrate import ref_phs_est as rpe
from pyrate import refpixel
from pyrate import shared
from pyrate import timeseries
from pyrate import vcm as vcm_module
from pyrate.compat import PyAPS_INSTALLED
from pyrate.shared import Ifg, create_tiles, \
    PrereadIfg, prepare_ifg, save_numpy_phase, get_projection_info

if PyAPS_INSTALLED:  # pragma: no cover
    # from pyrate import aps
    from pyrate.aps import check_aps_ifgs, aps_delay_required

MASTER_PROCESS = 0
log = logging.getLogger(__name__)


def get_tiles(ifg_path, rows, cols):
    """
    Break up the interferograms into tiles based on user supplied rows and columns.

    :param ifg_path: List of destination tifs
    :param rows: Number of rows to break each interferogram into
    :param cols: Number of columns to break each interferogram into

    :return tiles: List of shared.Tile instances
    """
    ifg = Ifg(ifg_path)
    ifg.open(readonly=True)
    tiles = create_tiles(ifg.shape, nrows=rows, ncols=cols)
    ifg.close()
    return tiles


def _join_dicts(dicts):
    """
    xxxx
    
    :param dicts: List of dictionaries to join

    :return assembled_dict: Dictionary after join
    """
    if dicts is None:  # pragma: no cover
        return
    assembled_dict = {k: v for D in dicts for k, v in D.items()}
    return assembled_dict


def create_ifg_dict(dest_tifs, params, tiles):
    """
    1. Convert interferogram phase data into numpy binary files.
    2. Save the preread_ifgs dictionary with information about the interferograms that are
    later used for fast loading of Ifg files in IfgPart class.

    :param dest_tifs: List of destination tifs
    :param params: Config dictionary
    :param tiles: List of all Tile instances

    :return preread_ifgs: Dictionary containing information regarding interferograms that are used downstream
    """
    ifgs_dict = {}
    process_tifs = mpiops.array_split(dest_tifs)
    save_numpy_phase(dest_tifs, tiles, params)
    for d in process_tifs:
        ifg = prepare_ifg(d, params)
        ifgs_dict[d] = PrereadIfg(path=d,
                                  nan_fraction=ifg.nan_fraction,
                                  master=ifg.master,
                                  slave=ifg.slave,
                                  time_span=ifg.time_span,
                                  nrows=ifg.nrows,
                                  ncols=ifg.ncols,
                                  metadata=ifg.meta_data)
        ifg.close()
    ifgs_dict = _join_dicts(mpiops.comm.allgather(ifgs_dict))

    preread_ifgs_file = join(params[cf.TMPDIR], 'preread_ifgs.pk')

    if mpiops.rank == MASTER_PROCESS:

        # add some extra information that's also useful later
        gt, md, wkt = get_projection_info(process_tifs[0])
        ifgs_dict['epochlist'] = algorithm.get_epochs(ifgs_dict)[0]
        ifgs_dict['gt'] = gt
        ifgs_dict['md'] = md
        ifgs_dict['wkt'] = wkt
        # dump ifgs_dict file for later use
        cp.dump(ifgs_dict, open(preread_ifgs_file, 'wb'))

    mpiops.comm.barrier()
    preread_ifgs = cp.load(open(preread_ifgs_file, 'rb'))
    log.info('Finished converting phase_data to numpy '
             'in process {}'.format(mpiops.rank))
    return preread_ifgs


def mst_calc(dest_tifs, params, tiles, preread_ifgs):
    """
    MPI function that control each process during MPI run
    Reference phase computation using method 2.

    :params dest_tifs: List of interferogram paths
    :param params: Parameters dictionary corresponding to config file
    :param tiles: List of all tiles used during MPI processes
    :param preread_ifgs: Dictionary containing interferogram characteristics for efficient computing
    
    :return xxxx
    """
    process_tiles = mpiops.array_split(tiles)

    def save_mst_tile(tile, i, preread_ifgs):
        """
        Convenient inner loop for MST tile saving.
        """
        if params[cf.NETWORKX_OR_MATLAB_FLAG] == 1:
            log.info('Calculating minimum spanning tree matrix '
                     'using NetworkX method')
            mst_tile = mst.mst_multiprocessing(tile, dest_tifs, preread_ifgs)
        elif params[cf.NETWORKX_OR_MATLAB_FLAG] == 0:
            raise ConfigException('Matlab mst not supported')
        else:
            raise ConfigException('Only NetworkX mst is supported')
            # mst_tile = mst.mst_multiprocessing(tile, dest_tifs, preread_ifgs)
        # locally save the mst_mat
        mst_file_process_n = join(
            params[cf.TMPDIR], 'mst_mat_{}.npy'.format(i))
        np.save(file=mst_file_process_n, arr=mst_tile)

    for t in process_tiles:
        save_mst_tile(t, t.index, preread_ifgs)
    log.info('finished mst calculation for process {}'.format(mpiops.rank))
    mpiops.comm.barrier()


def ref_pixel_calc(ifg_paths, params):
    """
    Reference pixel calculation setup.

    :param ifg_paths: List of interferogram paths
    :param params: Parameters dictionary corresponding to config file

    :return refx: Reference pixel x-coordinate
    :return refy: Reference pixel y-coordinate
    """
    # unlikely, but possible the refpixel can be (0,0)
    # check if there is a pre-specified reference pixel coord
    refx = params[cf.REFX]
    ifg = Ifg(ifg_paths[0])
    ifg.open(readonly=True)
    if refx > ifg.ncols - 1:
        msg = ('Supplied reference pixel X coordinate is greater than '
               'the number of ifg columns: {}').format(refx)
        raise ValueError(msg)

    refy = params[cf.REFY]
    if refy > ifg.nrows - 1:
        msg = ('Supplied reference pixel Y coordinate is greater than '
               'the number of ifg rows: {}').format(refy)
        raise ValueError(msg)

    if refx <= 0 or refy <= 0:  # if either zero or negative
        log.info('Searching for best reference pixel location')
        refy, refx = find_ref_pixel(ifg_paths, params)
        log.info('Selected reference pixel coordinate: '
                 '({}, {})'.format(refx, refy))
    else:  # pragma: no cover
        log.info('Reusing reference pixel from config file: '
                 '({}, {})'.format(refx, refy))
    ifg.close()
    return refx, refy


def find_ref_pixel(ifg_paths, params):
    """
    Find reference pixel using MPI Parameters.

    :param ifg_paths: List of interferogram paths
    :param params: Parameters dictionary corresponding to config file

    :return Tuple of (refy, refx).
    """
    half_patch_size, thresh, grid = refpixel.ref_pixel_setup(ifg_paths, params)
    process_grid = mpiops.array_split(grid)
    save_ref_pixel_blocks(process_grid, half_patch_size, ifg_paths, params)
    mean_sds = refpixel.ref_pixel_mpi(process_grid, half_patch_size,
                                      ifg_paths, thresh, params)
    mean_sds = mpiops.comm.gather(mean_sds, root=0)
    if mpiops.rank == MASTER_PROCESS:
        mean_sds = np.hstack(mean_sds)
    return mpiops.run_once(refpixel.filter_means, mean_sds, grid)


def save_ref_pixel_blocks(grid, half_patch_size, ifg_paths, params):
    """
    xxxx
    
    :param grid: List of tuples (y, x) corresponding reference pixel grids
    :param half_patch_size: Patch size in pixels corresponding to reference pixel grids
    :param ifg_paths: List of interferogram paths
    :param params: Parameters dictionary corresponding to config file
    
    :return xxxx
    """
    log.info('Saving ref pixel blocks')
    outdir = params[cf.TMPDIR]
    for pth in ifg_paths:
        ifg = Ifg(pth)
        ifg.open(readonly=True)
        ifg.nodata_value = params[cf.NO_DATA_VALUE]
        ifg.convert_to_nans()
        ifg.convert_to_mm()
        for y, x in grid:
            data = ifg.phase_data[y - half_patch_size:y + half_patch_size + 1,
                                  x - half_patch_size:x + half_patch_size + 1]

            data_file = join(outdir, 'ref_phase_data_{b}_{y}_{x}.npy'.format(
                b=os.path.basename(pth).split('.')[0], y=y, x=x))
            np.save(file=data_file, arr=data)
        ifg.close()
    log.info('Saved ref pixel blocks')


def orb_fit_calc(ifg_paths, params, preread_ifgs=None):
    """
    Orbital fit correction.

    :param ifg_paths: List of ifg paths
    :param params: Parameters dictionary corresponding to config file
    :param preread_ifgs: Dictionary containing information regarding interferograms

    :return xxxx
    """
    #log.info('Calculating orbfit correction')
    if params[cf.ORBITAL_FIT_METHOD] == 1:
        prcs_ifgs = mpiops.array_split(ifg_paths)
        orbital.remove_orbital_error(prcs_ifgs, params, preread_ifgs)
    else:
        # Here we do all the multilooking in one process, but in memory
        # can use multiple processes if we write data to disc during
        # remove_orbital_error step
        # A performance comparison should be made for saving multilooked
        # files on disc vs in memory single process multilooking
        if mpiops.rank == MASTER_PROCESS:
            orbital.remove_orbital_error(ifg_paths, params, preread_ifgs)
    mpiops.comm.barrier()
    #log.info('Finished orbfit calculation in process {}'.format(mpiops.rank))


def ref_phase_estimation(ifg_paths, params, refpx, refpy, preread_ifgs=None):
    """
    Reference phase estimation.

    :param ifg_paths: List of interferogram paths
    :param params: Parameters dictionary corresponding to config file
    :param refpx: Reference pixel x-coordinate
    :param refpy: Reference pixel y-coordinate
    :param preread_ifgs: Dictionary containing information regarding interferograms

    :return xxxx
    """
    # perform some checks on existing ifgs
    if preread_ifgs and mpiops.rank == MASTER_PROCESS:
        ifg_paths = sorted(preread_ifgs.keys())
        if rpe.check_ref_phs_ifgs(ifg_paths, preread_ifgs):
            return # return if True condition returned

    if params[cf.REF_EST_METHOD] == 1:
        # calculate phase sum for later use in ref phase method 1
        comp = phase_sum(ifg_paths, params)
        log.info('Computing reference phase via method 1')
        process_ref_phs = ref_phs_method1(ifg_paths, comp)
    elif params[cf.REF_EST_METHOD] == 2:
        log.info('Computing reference phase via method 2')
        process_ref_phs = ref_phs_method2(ifg_paths, params, refpx, refpy)
    else:
        raise ConfigException('Ref phase estimation method must be 1 or 2')

    # Save reference phase numpy arrays to disk
    ref_phs_file = join(params[cf.TMPDIR], 'ref_phs.npy')
    if mpiops.rank == MASTER_PROCESS:
        ref_phs = np.zeros(len(ifg_paths), dtype=np.float64)
        process_indices = mpiops.array_split(range(len(ifg_paths)))
        ref_phs[process_indices] = process_ref_phs
        for r in range(1, mpiops.size):  # pragma: no cover
            process_indices = mpiops.array_split(range(len(ifg_paths)), r)
            this_process_ref_phs = np.zeros(shape=len(process_indices),
                                            dtype=np.float64)
            mpiops.comm.Recv(this_process_ref_phs, source=r, tag=r)
            ref_phs[process_indices] = this_process_ref_phs
        np.save(file=ref_phs_file, arr=ref_phs)
    else:  # pragma: no cover
        # send reference phase data to master process
        mpiops.comm.Send(process_ref_phs, dest=MASTER_PROCESS,
                         tag=mpiops.rank)
    log.info('Completed reference phase estimation')


def ref_phs_method2(ifg_paths, params, refpx, refpy):
    """
    Reference phase computation using method 2.

    :param ifg_paths: List of interferogram paths
    :param params: Parameters dictionary corresponding to config file
    :param refpx: Reference pixel x-coordinate
    :param refpy: Reference pixel y-coordinate

    :return ref_phs: Array of reference phase of shape ifg.shape
    """
    half_chip_size = int(np.floor(params[cf.REF_CHIP_SIZE] / 2.0))
    chipsize = 2 * half_chip_size + 1
    thresh = chipsize * chipsize * params[cf.REF_MIN_FRAC]
    process_ifg_paths = mpiops.array_split(ifg_paths)

    def _inner(ifg_path):
        ifg = Ifg(ifg_path)
        ifg.open(readonly=False)
        phase_data = ifg.phase_data
        ref_ph = rpe.est_ref_phs_method2(phase_data,
                                         half_chip_size,
                                         refpx, refpy, thresh)
        phase_data -= ref_ph
        md = ifg.meta_data
        md[ifc.PYRATE_REF_PHASE] = ifc.REF_PHASE_REMOVED
        ifg.write_modified_phase(data=phase_data)
        ifg.close()
        return ref_ph

    ref_phs = np.array([_inner(p) for p in process_ifg_paths])
    log.info('Ref phase computed in process {}'.format(mpiops.rank))
    return ref_phs


def ref_phs_method1(ifg_paths, comp):
    """
    Reference phase computation using method 1.

    :param ifg_paths: List of interferogram paths
    :param comp: Array of phase sum of all interferograms of shape ifg.shape

    :return ref_phs: Array of reference phase of shape ifg.shape
    """

    def _inner(ifg_path):
        """
        Convenient inner loop.
        """
        ifg = Ifg(ifg_path)
        ifg.open(readonly=False)
        phase_data = ifg.phase_data
        ref_phase = rpe.est_ref_phs_method1(phase_data, comp)
        phase_data -= ref_phase
        md = ifg.meta_data
        md[ifc.PYRATE_REF_PHASE] = ifc.REF_PHASE_REMOVED
        ifg.write_modified_phase(data=phase_data)
        ifg.close()
        return ref_phase
    this_process_ifgs = mpiops.array_split(ifg_paths)
    ref_phs = np.array([_inner(ifg) for ifg in this_process_ifgs])
    log.info('Ref phase computed in process {}'.format(mpiops.rank))
    return ref_phs


def process_ifgs(ifg_paths, params, rows, cols):
    """
    Top level function to perform PyRate correction steps on given interferograms.

    :param ifg_paths: List of interferogram paths
    :param params: Parameters dictionary corresponding to config file
    :param rows: Number of rows to break each interferogram into
    :param cols: Number of columns to break each interferogram into
    
    :return xxxx
    """
    if mpiops.size > 1:
        params[cf.PARALLEL] = False

    tiles = mpiops.run_once(get_tiles, ifg_paths[0], rows, cols)
    preread_ifgs = create_ifg_dict(ifg_paths,
                                   params=params,
                                   tiles=tiles)

    mst_calc(ifg_paths, params, tiles, preread_ifgs)

    # Estimate reference pixel location
    # TODO: Skip this if reference phase already removed?
    refpx, refpy = ref_pixel_calc(ifg_paths, params)

    # remove APS delay here, and write aps delay removed ifgs to disc
    # TODO: fix PyAPS integration
    if PyAPS_INSTALLED and \
            aps_delay_required(ifg_paths, params):  # pragma: no cover
        # ifgs = aps.remove_aps_delay(ifg_paths, params)
        log.info('Finished APS delay correction')
        # make sure aps correction flags are consistent
        if params[cf.APS_CORRECTION]:
            check_aps_ifgs(ifg_paths)

    # Estimate and remove orbit errors
    orb_fit_calc(ifg_paths, params, preread_ifgs)

    # calc and remove reference phase
    ref_phase_estimation(ifg_paths, params, refpx, refpy, preread_ifgs)

    # calculate maxvar and alpha values
    maxvar = maxvar_alpha_calc(ifg_paths, params, preread_ifgs)

    # assemble variance-covariance matrix
    vcmt = vcm_calc(preread_ifgs, maxvar)
    save_numpy_phase(ifg_paths, tiles, params)

    if params[cf.TIME_SERIES_CAL]:
        timeseries_calc(ifg_paths, params, vcmt, tiles, preread_ifgs)

    # Calculate linear rate map
    linrate_calc(ifg_paths, params, vcmt, tiles, preread_ifgs)

    log.info('PyRate workflow completed')
    return (refpx, refpy), maxvar, vcmt


def linrate_calc(ifg_paths, params, vcmt, tiles, preread_ifgs):
    """
    MPI capable linrate calculation.

    :param ifg_paths: List of interferogram paths
    :param params: Parameters dictionary corresponding to config file
    :param vcmt: vcmt array
    :param tiles: List of all tiles used during MPI processes
    :param preread_ifgs: Dictionary containing interferogram characteristics for efficient computing
    
    :return xxxx
    """

    process_tiles = mpiops.array_split(tiles)
    log.info('Calculating linear rate map')
    output_dir = params[cf.TMPDIR]
    for t in process_tiles:
        log.info('Calculating linear rate of tile {}'.format(t.index))
        ifg_parts = [shared.IfgPart(p, t, preread_ifgs) for p in ifg_paths]
        mst_grid_n = np.load(os.path.join(output_dir,
                                          'mst_mat_{}.npy'.format(t.index)))
        rate, error, samples = linrate.linear_rate(ifg_parts, params,
                                                   vcmt, mst_grid_n)
        # declare file names
        np.save(file=os.path.join(output_dir,
                                  'linrate_{}.npy'.format(t.index)),
                arr=rate)
        np.save(file=os.path.join(output_dir,
                                  'linerror_{}.npy'.format(t.index)),
                arr=error)
        np.save(file=os.path.join(output_dir,
                                  'linsamples_{}.npy'.format(t.index)),
                arr=samples)
    mpiops.comm.barrier()


def maxvar_alpha_calc(ifg_paths, params, preread_ifgs):
    """
    MPI capable maxvar and vcmt computation.

    :param ifg_paths: List of interferogram paths
    :param params: Parameters dictionary corresponding to config file
    :param preread_ifgs: Dictionary containing interferogram characteristics for efficient computing

    :return maxvar: Array of shape (nifgs, 1)
    :return vcmt: Array of shape (nifgs, nifgs)
    """
    process_indices = mpiops.array_split(range(len(ifg_paths)))
    prcs_ifgs = mpiops.array_split(ifg_paths)
    process_maxvar = []
    for n, i in enumerate(prcs_ifgs):
        log.info('Fitting Covariance function for {} out of {} ifgs assigned '
                 'to this process, out of a total {} ifgs'.format(
                     n+1, len(prcs_ifgs), len(ifg_paths)))
        # TODO: cvd calculation is still pretty slow - revisit
        process_maxvar.append(vcm_module.cvd(i, params, calc_alpha=True,
                                             write_vals=True, save_acg=True)[0])
    if mpiops.rank == MASTER_PROCESS:
        maxvar = np.empty(len(ifg_paths), dtype=np.float64)
        maxvar[process_indices] = process_maxvar
        for i in range(1, mpiops.size):  # pragma: no cover
            rank_indices = mpiops.array_split(range(len(ifg_paths)), i)
            this_process_maxvar = np.empty(len(rank_indices),
                                           dtype=np.float64)
            mpiops.comm.Recv(this_process_maxvar, source=i, tag=i)
            maxvar[rank_indices] = this_process_maxvar
    else:  # pragma: no cover
        maxvar = np.empty(len(ifg_paths), dtype=np.float64)
        mpiops.comm.Send(np.array(process_maxvar, dtype=np.float64),
                         dest=MASTER_PROCESS, tag=mpiops.rank)
    return maxvar


def vcm_calc(preread_ifgs, maxvar):
    """
    Temporal Variance-Covariance Matrix computation.

    :param preread_ifgs: Dictionary containing interferograms characteristics for efficient computing
    :param maxvar: Array of shape (nifgs, 1)

    :return vcmt: Array of shape (nifgs, nifgs)
    """
    maxvar = mpiops.comm.bcast(maxvar, root=0)
    log.info('Assembling Temporal Variance-Covariance Matrix')
    vcmt = mpiops.run_once(vcm_module.get_vcmt, preread_ifgs, maxvar)
    return vcmt


def phase_sum(ifg_paths, params):
    """
    Save phase data and phs_sum used in the reference phase estimation.

    :param ifg_paths: List of paths to interferograms
    :param params: Config dictionary
    
    :return xxxx
    """
    p_paths = mpiops.array_split(ifg_paths)
    ifg = Ifg(p_paths[0])
    ifg.open(readonly=True)
    shape = ifg.shape
    phs_sum = np.zeros(shape=shape, dtype=np.float64)
    ifg.close()

    for d in p_paths:
        ifg = Ifg(d)
        ifg.open()
        ifg.nodata_value = params[cf.NO_DATA_VALUE]
        phs_sum += ifg.phase_data
        ifg.close()

    if mpiops.rank == MASTER_PROCESS:
        phase_sum_all = phs_sum
        # loop is better for memory
        for i in range(1, mpiops.size):  # pragma: no cover
            phs_sum = np.zeros(shape=shape, dtype=np.float64)
            mpiops.comm.Recv(phs_sum, source=i, tag=i)
            phase_sum_all += phs_sum
        comp = np.isnan(phase_sum_all)  # this is the same as in Matlab
        comp = np.ravel(comp, order='F')  # this is the same as in Matlab
    else:  # pragma: no cover
        comp = None
        mpiops.comm.Send(phs_sum, dest=0, tag=mpiops.rank)

    comp = mpiops.comm.bcast(comp, root=0)
    return comp


def timeseries_calc(ifg_paths, params, vcmt, tiles, preread_ifgs):
    """
    Time series calculation.

    :param ifg_paths: List of interferogram paths
    :param params: Parameters dictionary corresponding to config file
    :param vcmt: vcmt array
    :param tiles: List of all tiles used during MPI processes
    :param preread_ifgs: Dictionary containing interferogram characteristics for efficient computing

    :return xxxx
    """
    process_tiles = mpiops.array_split(tiles)
    log.info('Calculating time series')
    output_dir = params[cf.TMPDIR]
    for t in process_tiles:
        log.info('Calculating time series for tile {}'.format(t.index))
        ifg_parts = [shared.IfgPart(p, t, preread_ifgs) for p in ifg_paths]
        mst_tile = np.load(os.path.join(output_dir,
                                        'mst_mat_{}.npy'.format(t.index)))
        res = timeseries.time_series(ifg_parts, params, vcmt, mst_tile)
        tsincr, tscum, _ = res
        np.save(file=os.path.join(output_dir, 'tsincr_{}.npy'.format(t.index)),
                arr=tsincr)
        np.save(file=os.path.join(output_dir, 'tscuml_{}.npy'.format(t.index)),
                arr=tscum)
    mpiops.comm.barrier()


def main(config_file, rows, cols):  # pragma: no cover
    """ linear rate and timeseries execution starts here """
    _, dest_paths, pars = cf.get_ifg_paths(config_file)
    process_ifgs(sorted(dest_paths), pars, rows, cols)
