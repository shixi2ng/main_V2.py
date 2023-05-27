import gdal
import sys
import pandas as pd
import numpy as np
import os
import zipfile
import shutil
import scipy.sparse as sp
import copy
import time
from basic_function import Path
import basic_function as bf
import concurrent.futures
from itertools import repeat
import datetime
import traceback
from GEDI_toolbox import GEDI_main as gedi
from scipy import sparse as sm
from Sentinel2_toolbox.utils import retrieve_srs, write_raster, remove_all_file_and_folder, link_GEDI_inform, cf2phemetric_dc, \
    link_GEDI_pheinform, get_index_by_date, seven_para_logistic_function, two_term_fourier, curfit4bound_annual, curfit_pd2raster, link_GEDI_accdenvinform, get_base_denv
from Sentinel2_toolbox.built_in_index import built_in_index
from Sentinel2_toolbox.Sentinel_main_V2 import Sentinel2_dc, Sentinel2_ds
from NDsm import NDSparseMatrix
import json
from tqdm.auto import tqdm


def seven_para_logistic_function(x, m1, m2, m3, m4, m5, m6, m7):
    return m1 + (m2 - m7 * x) * ((1 / (1 + np.exp((m3 - x) / m4))) - (1 / (1 + np.exp((m5 - x) / m6))))


def two_term_fourier(x, a0, a1, b1, a2, b2, w):
    return a0 + a1 * np.cos(w * x) + b1 * np.sin(w * x) + a2 * np.cos(2 * w * x)+b2 * np.sin(2 * w * x)


class Denv_dc(object):

    ####################################################################################################
    # Denv dc represents "Daily Environment Datacube"
    # It normally contains data like daily temperature and daily radiation, etc
    # And stack into a 3-D datacube type.
    # Currently, it was integrated into the NCEI and MODIS FPAR toolbox as a output datatype.
    ####################################################################################################

    def __init__(self, Denv_dc_filepath, work_env=None, autofill=True):

        # Check the phemetric path
        self.Denv_dc_filepath = bf.Path(Denv_dc_filepath).path_name

        # Init key var
        self.ROI_name, self.ROI, self.ROI_tif, self.ROI_array = None, None, None, None
        self.index, self.Datatype, self.coordinate_system = None, None, None
        self.dc_group_list, self.tiles, self.oritif_folder = None, None, None
        self.sdc_factor, self.sparse_matrix, self.size_control_factor, self.huge_matrix = False, False, False, False
        self.Denv_factor, self.timescale, self.timerange = False, None, None
        self.compete_doy_list = []

        # Check work env
        if work_env is not None:
            self._work_env = Path(work_env).path_name
        else:
            self._work_env = Path(os.path.dirname(os.path.dirname(self.Denv_dc_filepath))).path_name
        self.root_path = Path(os.path.dirname(os.path.dirname(self._work_env))).path_name

        # Define the basic var name
        self._fund_factor = ('ROI_name', 'index', 'Datatype', 'ROI', 'ROI_array', 'sdc_factor',
                             'coordinate_system', 'oritif_folder', 'ROI_tif', 'sparse_matrix',
                             'huge_matrix', 'size_control_factor', 'dc_group_list', 'tiles', 'timescale', 'timerange')

        # Read the metadata file
        metadata_file = bf.file_filter(self.Denv_dc_filepath, ['metadata.json'])
        if len(metadata_file) == 0:
            raise ValueError('There has no valid sdc or the metadata file of the sdc was missing!')
        elif len(metadata_file) > 1:
            raise ValueError('There has more than one metadata file in the dir')
        else:
            try:
                # Load json metadata
                with open(metadata_file[0]) as js_temp:
                    dc_metadata = json.load(js_temp)

                if not isinstance(dc_metadata, dict):
                    raise Exception('Please make sure the metadata file is a dictionary constructed in python!')
                else:
                    # Determine whether this datacube is Denv or not
                    if 'Denv_factor' not in dc_metadata.keys():
                        raise TypeError('The Denv factor was lost or it is not a Denv datacube!')
                    elif dc_metadata['Denv_factor'] is False:
                        raise TypeError(f'{self.Denv_dc_filepath} is not a Denv datacube!')
                    elif dc_metadata['Denv_factor'] is True:
                        self.Denv_factor = dc_metadata['Denv_factor']
                    else:
                        raise TypeError('The Denv factor was under wrong type!')

                    for dic_name in self._fund_factor:
                        if dic_name not in dc_metadata.keys():
                            raise Exception(f'The {dic_name} is not in the dc metadata, double check!')
                        else:
                            self.__dict__[dic_name] = dc_metadata[dic_name]

            except:
                raise Exception('Something went wrong when reading the metadata!')

        # Check the timescale and timerange is consistency or not
        if self.timescale == 'year' and len(str(self.timerange)) != 4:
            raise Exception('The annual time range should be in YYYY format')
        elif self.timescale == 'month' and len(str(self.timerange)) != 6:
            raise Exception('The monthly time range should be in YYYYMM format')
        elif self.timescale == 'all' and len(str(self.timerange)) != '.TIF':
            raise Exception('The all time range should be in .TIF format')
        elif self.timescale is None or self.timerange is None:
            raise Exception('The timescale and timerange para is not properly assigned!')

        start_time = time.time()
        print(f'Start loading the Denv dc of \033[1;31m{self.index}\033[0m in the \033[1;34m{self.ROI_name}\033[0m')

        # Read doy or date file of the Datacube
        try:
            if self.sdc_factor is True:
                # Read doylist
                doy_file = bf.file_filter(self.Denv_dc_filepath, ['doy.npy'])
                if len(doy_file) == 0:
                    raise ValueError('There has no valid doy file or file was missing!')
                elif len(doy_file) > 1:
                    raise ValueError('There has more than one doy file in the dc dir')
                else:
                    sdc_doylist = np.load(doy_file[0], allow_pickle=True)
                    self.sdc_doylist = [int(sdc_doy) for sdc_doy in sdc_doylist]
            else:
                raise TypeError('Please input as a sdc')
        except:
            raise Exception('Something went wrong when reading the doy and date list!')
        self.year_domain = set([int(np.floor(temp)) for temp in self.sdc_doylist])

        # Compete doy list
        if self.timescale == 'year':
            date_start = datetime.date(year=self.timerange, month=1, day=1).toordinal()
            date_end = datetime.date(year=self.timerange + 1, month=1, day=1).toordinal()
            compete_doy_list = [datetime.date.fromordinal(date_temp).strftime('%Y%m%d') for date_temp in range(date_start, date_end)]
            self.compete_doy_list = bf.date2doy(compete_doy_list)
        elif self.timescale == 'month':
            year_temp = int(np.floor(self.timerange/100))
            month_temp = int(np.mod(self.timerange, 100))
            date_start = datetime.date(year=year_temp, month=month_temp, day=1).toordinal()
            date_end = datetime.date(year=year_temp, month=month_temp + 1, day=1).toordinal()
            compete_doy_list = [datetime.date.fromordinal(date_temp).strftime('%Y%m%d') for date_temp in range(date_start, date_end)]
            self.compete_doy_list = bf.date2doy(compete_doy_list)
        elif self.timescale == 'all':
            date_min, date_max = bf.doy2date(min(self.sdc_doylist)), bf.doy2date(max(self.sdc_doylist))
            date_min = datetime.date(year=int(np.floor(date_min/1000)), month=1, day=1).toordinal() + np.mod(date_min, 1000) - 1
            date_max = datetime.date(year=int(np.floor(date_max/1000)), month=1, day=1).toordinal() + np.mod(date_max, 1000)
            compete_doy_list = [datetime.date.fromordinal(date_temp).strftime('%Y%m%d') for date_temp in range(date_min, date_max)]
            self.compete_doy_list = bf.date2doy(compete_doy_list)

        # Read the Denv datacube
        try:
            if self.sparse_matrix and self.huge_matrix:
                if os.path.exists(self.Denv_dc_filepath + f'{self.index}_Denv_datacube\\'):
                    self.dc = NDSparseMatrix().load(self.Denv_dc_filepath + f'{self.index}_Denv_datacube\\')
                else:
                    raise Exception('Please double check the code if the sparse huge matrix is generated properly')
            elif not self.huge_matrix:
                self.dc_filename = bf.file_filter(self.Denv_dc_filepath, ['Denv_datacube.npy'])
                if len(self.dc_filename) == 0:
                    raise ValueError('There has no valid dc or the dc was missing!')
                elif len(self.dc_filename) > 1:
                    raise ValueError('There has more than one date file in the dc dir')
                else:
                    self.dc = np.load(self.dc_filename[0], allow_pickle=True)
            elif self.huge_matrix and not self.sparse_matrix:
                self.dc_filename = bf.file_filter(self.Denv_dc_filepath, ['Denv_datacube', '.npy'], and_or_factor='and')
        except:
            raise Exception('Something went wrong when reading the datacube!')

        if autofill is True and len(self.compete_doy_list) > len(self.sdc_doylist):
            self._autofill_Denv_DC()
        elif len(self.compete_doy_list) < len(self.sdc_doylist):
            raise Exception('Code has issues in the Denc autofill procedure!')

        # autotrans sparse matrix
        if self.sparse_matrix and self.dc._matrix_type == sm.coo_matrix:
            self._autotrans_sparse_matrix()

        # Size calculation and shape definition
        self.dc_XSize, self.dc_YSize, self.dc_ZSize = self.dc.shape[1], self.dc.shape[0], self.dc.shape[2]
        if self.dc_ZSize != len(self.sdc_doylist):
            raise TypeError('The Denv datacube is not consistent with the doy list')

        print(f'Finish loading the Denv dc of \033[1;31m{self.index}\033[0m for the \033[1;34m{self.ROI_name}\033[0m using \033[1;31m{str(time.time() - start_time)}\033[0ms')

    def __sizeof__(self):
        return self.dc.__sizeof__() + self.sdc_doylist.__sizeof__()

    def _autofill_Denv_DC(self):
        for date_temp in self.compete_doy_list:
            if date_temp not in self.sdc_doylist:
                if date_temp == self.compete_doy_list[0]:
                    date_merge = self.compete_doy_list[1]
                    if self.sparse_matrix:
                        self.dc.add_layer(self.dc.SM_group[date_merge], date_temp, 0)
                    else:
                        self.dc = np.insert(self.dc, 0, values=self.dc[:,:,0], axis=2)
                    self.sdc_doylist.insert(0, date_temp)
                elif date_temp == self.compete_doy_list[-1]:
                    date_merge = self.compete_doy_list[-2]
                    if self.sparse_matrix:
                        self.dc.add_layer(self.dc.SM_group[date_merge], date_temp, -1)
                    else:
                        self.dc = np.insert(self.dc, 0, values=self.dc[:,:,-1], axis=2)
                    self.sdc_doylist.insert(-1, date_temp)
                else:
                    date_beg, date_end, _beg, _end = None, None, None, None
                    for _ in range(1, 30):
                        ordinal_date = datetime.date(year=int(np.floor(bf.doy2date(date_temp)/10000)), month=int(np.floor(np.mod(bf.doy2date(date_temp), 10000)/100)), day=int(np.mod(bf.doy2date(date_temp), 100))).toordinal()
                        if date_beg is None:
                            date_out = bf.date2doy(int(datetime.date.fromordinal(ordinal_date - _).strftime('%Y%m%d')))
                            date_beg = date_out if date_out in self.sdc_doylist else None
                            _beg = _ if date_out in self.sdc_doylist else None

                        if date_end is None:
                            date_out = bf.date2doy(int(datetime.date.fromordinal(ordinal_date + _).strftime('%Y%m%d')))
                            date_end = date_out if date_out in self.sdc_doylist else None
                            _end = _ if date_out in self.sdc_doylist else None

                        if date_end is not None and date_beg is not None:
                            break

                    if isinstance(self.dc, NDSparseMatrix):
                        type_temp = type(self.dc.SM_group[date_beg])
                        array_beg = self.dc.SM_group[date_beg].toarray()
                        array_end = self.dc.SM_group[date_end].toarray()
                        dtype_temp = array_end.dtype
                        array_beg = array_beg.astype(np.float32)
                        array_end = array_end.astype(np.float32)
                        array_out = array_beg + (array_end - array_beg) * _beg / (_beg + _end)
                        array_out = array_out.astype(dtype_temp)
                        array_out = type_temp(array_out)
                        self.dc.add_layer(array_out, date_temp, self.compete_doy_list.index(date_temp))
                    else:
                        array_beg = self.dc[:, :, date_beg]
                        array_end = self.dc[:, :, date_end]
                        array_beg = array_beg.astype(np.float32)
                        array_end = array_end.astype(np.float32)
                        array_out = array_beg + (array_end - array_beg) * _beg / (_beg + _end)
                        self.dc = np.insert(self.dc, self.compete_doy_list.index(date_temp), values=array_out.reshape([array_out.shape[0], array_out.shape[1], 1]), axis=2)
                    self.sdc_doylist.insert(self.compete_doy_list.index(date_temp), date_temp)

        if self.sdc_doylist != self.compete_doy_list:
            raise Exception('Error occurred during the autofill for the Denv DC!')

        self.save(self.Denv_dc_filepath)
        self.__init__(self.Denv_dc_filepath)

    def save(self, output_path: str):

        start_time = time.time()
        print(f'Start saving the sdc of \033[1;31m{self.index}\033[0m in the \033[1;34m{self.ROI_name}\033[0m')

        output_path = bf.Path(output_path).path_name
        bf.create_folder(output_path) if not os.path.exists(output_path) else None

        metadata_dic = {'ROI_name': self.ROI_name, 'index': self.index, 'Datatype': self.Datatype, 'ROI': self.ROI,
                        'ROI_array': self.ROI_array, 'ROI_tif': self.ROI_tif, 'sdc_factor': self.sdc_factor,
                        'coordinate_system': self.coordinate_system, 'sparse_matrix': self.sparse_matrix, 'huge_matrix': self.huge_matrix,
                        'size_control_factor': self.size_control_factor, 'oritif_folder': self.oritif_folder, 'dc_group_list': self.dc_group_list,
                        'tiles': self.tiles, 'timescale': self.timescale, 'timerange': self.timerange, 'Denv_factor': self.Denv_factor}
        doy = self.sdc_doylist
        np.save(f'{output_path}doy.npy', doy)
        with open(f'{output_path}metadata.json', 'w') as js_temp:
            json.dump(metadata_dic, js_temp)

        if self.sparse_matrix:
            self.dc.save(f'{output_path}{str(self.index)}_Denv_datacube\\')
        else:
            np.save(f'{output_path}{str(self.index)}_Denv_datacube.npy', self.dc)

        print(f'Finish saving the sdc of \033[1;31m{self.index}\033[0m for the \033[1;34m{self.ROI_name}\033[0m using \033[1;31m{str(time.time() - start_time)}\033[0ms')

    def _autotrans_sparse_matrix(self):

        if not isinstance(self.dc, NDSparseMatrix):
            raise TypeError('The autotrans sparse matrix is specified for the NDsm!')

        for _ in self.dc.SM_namelist:
            if isinstance(self.dc.SM_group[_], sm.coo_matrix):
                self.dc.SM_group[_] = sm.csr_matrix(self.dc.SM_group[_])
                self.dc._update_size_para()
        self.save(self.Denv_dc_filepath)


class Phemetric_dc(object):

    ####################################################################################################
    # Phemetric_dc represents "Phenological Metric Datacube"
    # It normally contains data like phenological parameters derived from the curve fitting, etc
    # And stack into a 3-D datacube type.
    # Currently, it was integrated into the Sentinel_dcs as a output data for phenological generation.
    ####################################################################################################

    def __init__(self, phemetric_filepath, work_env=None):

        # Check the phemetric path
        self.Phemetric_dc_filepath = bf.Path(phemetric_filepath).path_name

        # Init key var
        self.ROI_name, self.ROI, self.ROI_tif = None, None, None
        self.index, self.Datatype, self.coordinate_system = None, None, None
        self.dc_group_list, self.tiles = None, None
        self.sdc_factor, self.sparse_matrix, self.size_control_factor, self.huge_matrix = False, False, False, False
        self.Phemetric_factor, self.pheyear = False, None
        self.curfit_dic = {}

        # Init protected var
        self._support_pheme_list = ['SOS', 'EOS', 'trough_vi', 'peak_vi', 'peak_doy', 'GR', 'DR', 'DR2']

        # Check work env
        if work_env is not None:
            self._work_env = Path(work_env).path_name
        else:
            self._work_env = Path(os.path.dirname(os.path.dirname(self.Phemetric_dc_filepath))).path_name
        self.root_path = Path(os.path.dirname(os.path.dirname(self._work_env))).path_name

        # Define the basic var name
        self._fund_factor = ('ROI_name', 'index', 'Datatype', 'ROI', 'ROI_array', 'curfit_dic', 'pheyear',
                             'coordinate_system', 'oritif_folder', 'ROI_tif', 'sparse_matrix',
                             'huge_matrix', 'size_control_factor', 'dc_group_list', 'tiles')

        # Read the metadata file
        metadata_file = bf.file_filter(self.Phemetric_dc_filepath, ['metadata.json'])
        if len(metadata_file) == 0:
            raise ValueError('There has no valid sdc or the metadata file of the sdc was missing!')
        elif len(metadata_file) > 1:
            raise ValueError('There has more than one metadata file in the dir')
        else:
            try:
                with open(metadata_file[0]) as js_temp:
                    dc_metadata = json.load(js_temp)

                if not isinstance(dc_metadata, dict):
                    raise Exception('Please make sure the metadata file is a dictionary constructed in python!')
                else:
                    # Determine whether this datacube is Phemetric or not
                    if 'Phemetric_factor' not in dc_metadata.keys():
                        raise TypeError('The Phemetric_factor was lost or it is not a Phemetric datacube!')
                    elif dc_metadata['Phemetric_factor'] is False:
                        raise TypeError(f'{self.Phemetric_dc_filepath} is not a Phemetric datacube!')
                    elif dc_metadata['Phemetric_factor'] is True:
                        self.Phemetric_factor = dc_metadata['Phemetric_factor']
                    else:
                        raise TypeError('The Phemetric factor was under wrong type!')

                    for dic_name in self._fund_factor:
                        if dic_name not in dc_metadata.keys():
                            raise Exception(f'The {dic_name} is not in the dc metadata, double check!')
                        else:
                            self.__dict__[dic_name] = dc_metadata[dic_name]
            except:
                raise Exception('Something went wrong when reading the metadata!')

        start_time = time.time()
        print(f'Start loading the Phemetric datacube of {str(self.pheyear)} \033[1;31m{self.index}\033[0m in the \033[1;34m{self.ROI_name}\033[0m')

        # Read paraname file of the Phemetric datacube
        try:
            if self.Phemetric_factor is True:
                # Read paraname
                paraname_file = bf.file_filter(self.Phemetric_dc_filepath, ['paraname.npy'])
                if len(paraname_file) == 0:
                    raise ValueError('There has no paraname file or file was missing!')
                elif len(paraname_file) > 1:
                    raise ValueError('There has more than one paraname file in the Phemetric datacube dir')
                else:
                    paraname_list = np.load(paraname_file[0], allow_pickle=True)
                    self.paraname_list = [paraname for paraname in paraname_list]
            else:
                raise TypeError('Please input as a Phemetric datacube')
        except:
            raise Exception('Something went wrong when reading the paraname list!')

        # Read func dic
        try:
            if self.sparse_matrix and self.huge_matrix:
                if os.path.exists(self.Phemetric_dc_filepath + f'{self.index}_Phemetric_datacube\\'):
                    self.dc = NDSparseMatrix().load(self.Phemetric_dc_filepath + f'{self.index}_Phemetric_datacube\\')
                else:
                    raise Exception('Please double check the code if the sparse huge matrix is generated properly')
            elif not self.huge_matrix:
                self.dc_filename = bf.file_filter(self.Phemetric_dc_filepath, ['Phemetric_datacube.npy'])
                if len(self.dc_filename) == 0:
                    raise ValueError('There has no valid Phemetric datacube or the dc was missing!')
                elif len(self.dc_filename) > 1:
                    raise ValueError('There has more than one data file in the dc dir')
                else:
                    self.dc = np.load(self.dc_filename[0], allow_pickle=True)
            elif self.huge_matrix and not self.sparse_matrix:
                self.dc_filename = bf.file_filter(self.Phemetric_dc_filepath, ['sequenced_datacube', '.npy'], and_or_factor='and')
        except:
            raise Exception('Something went wrong when reading the Phemetric datacube!')

        # autotrans sparse matrix
        if self.sparse_matrix and self.dc._matrix_type == sm.coo_matrix:
            self._autotrans_sparse_matrix()

        # Drop duplicate layers
        self._drop_duplicate_layers()

        # Size calculation and shape definition
        self.dc_XSize, self.dc_YSize, self.dc_ZSize = self.dc.shape[1], self.dc.shape[0], self.dc.shape[2]
        if self.dc_ZSize != len(self.paraname_list):
            raise TypeError('The Phemetric datacube is not consistent with the paraname file')

        print(f'Finish loading the Phemetric datacube of {str(self.pheyear)} \033[1;31m{self.index}\033[0m for the \033[1;34m{self.ROI_name}\033[0m using \033[1;31m{str(time.time() - start_time)}\033[0ms')

    def _autotrans_sparse_matrix(self):

        if not isinstance(self.dc, NDSparseMatrix):
            raise TypeError('The autotrans sparse matrix is specified for the NDsm!')

        for _ in self.dc.SM_namelist:
            if isinstance(self.dc.SM_group[_], sm.coo_matrix):
                self.dc.SM_group[_] = sm.csr_matrix(self.dc.SM_group[_])
                self.dc._update_size_para()
        self.save(self.Phemetric_dc_filepath)

    def _drop_duplicate_layers(self):
        for _ in self.paraname_list:
            if len([t for t in self.paraname_list if t == _]) != 1:
                pos = [tt for tt in range(len(self.paraname_list)) if self.paraname_list[tt] == _]
                if isinstance(self.dc, NDSparseMatrix):
                    self.dc.SM_namelist.pop(pos[-1])
                    self.paraname_list.pop(pos[-1])
                    self.dc._update_size_para()
                else:
                    self.dc = np.delete(self.dc, pos[-1], axis=2)
                    self.paraname_list.pop(pos[-1])

    def __sizeof__(self):
        return self.dc.__sizeof__() + self.paraname_list.__sizeof__()

    def calculate_phemetrics(self, pheme_list: list, save2phemedc: bool = True):

        pheme_list_temp = copy.copy(pheme_list)
        for pheme_temp in pheme_list:
            if pheme_temp not in self._support_pheme_list:
                raise ValueError(f'The {pheme_temp} is not supported')
            elif f'{self.pheyear}_{pheme_temp}' in self.paraname_list:
                pheme_list_temp.remove(pheme_temp)
        pheme_list = pheme_list_temp

        if self.curfit_dic['CFM'] == 'SPL':
            for pheme_temp in pheme_list:
                if pheme_temp == 'SOS':
                    if isinstance(self.dc, NDSparseMatrix):
                        self._add_layer(self.dc.SM_group[f'{str(self.pheyear)}_para_2'], 'SOS')
                    else:
                        self._add_layer(self.dc[self.paraname_list.index(f'{str(self.pheyear)}_para_2')], 'SOS')
                elif pheme_temp == 'EOS':
                    if isinstance(self.dc, NDSparseMatrix):
                        self._add_layer(self.dc.SM_group[f'{str(self.pheyear)}_para_4'], 'EOS')
                    else:
                        self._add_layer(self.dc[self.paraname_list.index(f'{str(self.pheyear)}_para_4')], 'EOS')
                elif pheme_temp == 'trough_vi':
                    if isinstance(self.dc, NDSparseMatrix):
                        self._add_layer(self.dc.SM_group[f'{str(self.pheyear)}_para_0'], 'trough_vi')
                    else:
                        self._add_layer(self.dc[self.paraname_list.index(f'{str(self.pheyear)}_para_0')], 'trough_vi')
                elif pheme_temp == 'peak_vi':
                    para_dic = {}
                    for para_num in range(self.curfit_dic['para_num']):
                        if isinstance(self.dc, NDSparseMatrix):
                            para_dic[para_num] = copy.copy(self.dc.SM_group[f'{str(self.pheyear)}_para_{str(para_num)}'].toarray())
                        else:
                            para_dic[para_num] = copy.copy(self.dc[self.paraname_list.index(f'{str(self.pheyear)}_para_{str(para_num)}')])

                    try:
                        peak_vi_array = np.zeros([para_dic[0].shape[0], para_dic[0].shape[1], 365])
                        for _ in range(peak_vi_array.shape[2]):
                            peak_vi_array[:, :, _] = _ + 1
                        peak_vi_array[para_dic[0] == 0] = np.nan
                        peak_vi_array = np.nanmax(seven_para_logistic_function(peak_vi_array, para_dic[0][y_temp, x_temp], para_dic[1][y_temp, x_temp], para_dic[2][y_temp, x_temp], para_dic[3][y_temp, x_temp], para_dic[4][y_temp, x_temp], para_dic[5][y_temp, x_temp], para_dic[6][y_temp, x_temp]), axis=2)
                        peak_vi_array[np.isnan(peak_vi_array)] = 0
                    except MemoryError:
                        peak_vi_array = copy.copy(para_dic[0])
                        peak_vi_array[peak_vi_array != 0] = -1
                        for y_temp in range(para_dic[0].shape[0]):
                            for x_temp in range(para_dic[0].shape[1]):
                                if peak_vi_array[y_temp, x_temp] == -1:
                                    peak_vi_array[y_temp, x_temp] = np.max(seven_para_logistic_function(np.linspace(1, 365, 365), para_dic[0][y_temp, x_temp], para_dic[1][y_temp, x_temp], para_dic[2][y_temp, x_temp], para_dic[3][y_temp, x_temp], para_dic[4][y_temp, x_temp], para_dic[5][y_temp, x_temp], para_dic[6][y_temp, x_temp]))
                        peak_vi_array[peak_vi_array == -1] = 0

                    self._add_layer(peak_vi_array, 'peak_vi')

                elif pheme_temp == 'peak_doy':
                    para_dic = {}
                    for para_num in range(self.curfit_dic['para_num']):
                        if isinstance(self.dc, NDSparseMatrix):
                            para_dic[para_num] = copy.copy(self.dc.SM_group[f'{str(self.pheyear)}_para_{str(para_num)}'].toarray())
                        else:
                            para_dic[para_num] = copy.copy(self.dc[self.paraname_list.index(f'{str(self.pheyear)}_para_{str(para_num)}')])

                    peak_doy_array = copy.copy(para_dic[0])
                    peak_doy_array[peak_doy_array != 0] = -1
                    for y_temp in range(para_dic[0].shape[0]):
                        for x_temp in range(para_dic[0].shape[1]):
                            if peak_doy_array[y_temp, x_temp] == -1:
                                peak_doy_array[y_temp, x_temp] = np.argmax(seven_para_logistic_function(np.linspace(1, 365, 365), para_dic[0][y_temp, x_temp], para_dic[1][y_temp, x_temp], para_dic[2][y_temp, x_temp], para_dic[3][y_temp, x_temp], para_dic[4][y_temp, x_temp], para_dic[5][y_temp, x_temp], para_dic[6][y_temp, x_temp])) + 1
                    peak_doy_array[peak_doy_array == -1] = 0

                    self._add_layer(peak_doy_array, 'peak_doy')

                elif pheme_temp == 'GR':
                    if isinstance(self.dc, NDSparseMatrix):
                        self._add_layer(self.dc.SM_group[f'{str(self.pheyear)}_para_3'], 'GR')
                    else:
                        self._add_layer(self.dc[self.paraname_list.index(f'{str(self.pheyear)}_para_3')], 'GR')
                elif pheme_temp == 'DR':
                    if isinstance(self.dc, NDSparseMatrix):
                        self._add_layer(self.dc.SM_group[f'{str(self.pheyear)}_para_5'], 'DR')
                    else:
                        self._add_layer(self.dc[self.paraname_list.index(f'{str(self.pheyear)}_para_5')], 'DR')
                elif pheme_temp == 'DR2':
                    if isinstance(self.dc, NDSparseMatrix):
                        self._add_layer(self.dc.SM_group[f'{str(self.pheyear)}_para_6'], 'DR2')
                    else:
                        self._add_layer(self.dc[self.paraname_list.index(f'{str(self.pheyear)}_para_6')], 'DR2')

        else:
            pass

        if save2phemedc:
            self.save(self.Phemetric_dc_filepath)
            self.__init__(self.Phemetric_dc_filepath)
        else:
            # Size calculation and shape definition
            self.dc_XSize, self.dc_YSize, self.dc_ZSize = self.dc.shape[1], self.dc.shape[0], self.dc.shape[2]
            if self.dc_ZSize != len(self.paraname_list) or self.dc_ZSize != self.curfit_dic['para_num']:
                raise TypeError('The Phemetric datacube is not consistent with the paraname file')

    def _add_layer(self, array, layer_name: str):

        # Process the layer name
        if not isinstance(layer_name, str):
            raise TypeError('Please input the adding layer name as a string！')
        elif not layer_name.startswith(str(self.pheyear)):
            layer_name = str(self.pheyear) + '_' + layer_name

        if self.sparse_matrix:
            sparse_type = type(self.dc.SM_group[self.dc.SM_namelist[0]])
            if not isinstance(array, sparse_type):
                try:
                    array = type(self.dc.SM_group[self.dc.SM_namelist[0]])(array)
                except:
                    raise Exception(f'The adding layer {layer_name} cannot be converted to the data type')
            try:
                self.dc.add_layer(array, layer_name, self.dc.shape[2])
                self.paraname_list.append(layer_name)
            except:
                raise Exception('Some error occurred during the add layer within a phemetric dc')
        else:
            try:
                self.dc = np.concatenate((self.dc, array.reshape(array.shape[0], array.shape[1], 1)), axis=2)
                self.paraname_list.append(layer_name)
            except:
                raise Exception('Some error occurred during the add layer within a phemetric dc')

    def save(self, output_path: str):
        start_time = time.time()
        print(f'Start saving the Phemetric datacube of \033[1;31m{self.index}\033[0m in the \033[1;34m{self.ROI_name}\033[0m')

        if not os.path.exists(output_path):
            bf.create_folder(output_path)
        output_path = bf.Path(output_path).path_name

        metadata_dic = {'ROI_name': self.ROI_name, 'index': self.index, 'Datatype': self.Datatype, 'ROI': self.ROI,
                        'ROI_array': self.ROI_array, 'ROI_tif': self.ROI_tif, 'sdc_factor': self.sdc_factor,
                        'coordinate_system': self.coordinate_system, 'sparse_matrix': self.sparse_matrix, 'huge_matrix': self.huge_matrix,
                        'size_control_factor': self.size_control_factor, 'oritif_folder': self.oritif_folder, 'dc_group_list': self.dc_group_list, 'tiles': self.tiles,
                        'pheyear': self.pheyear, 'curfit_dic': self.curfit_dic, 'Phemetric_factor': self.Phemetric_factor}

        paraname = self.paraname_list
        np.save(f'{output_path}paraname.npy', paraname)
        with open(f'{output_path}metadata.json', 'w') as js_temp:
            json.dump(metadata_dic, js_temp)

        if self.sparse_matrix:
            self.dc.save(f'{output_path}{str(self.index)}_Phemetric_datacube\\')
        else:
            np.save(f'{output_path}{str(self.index)}_Phemetric_datacube.npy', self.dc)

        print(f'Finish saving the Phemetric datacube of \033[1;31m{self.index}\033[0m for the \033[1;34m{self.ROI_name}\033[0m using \033[1;31m{str(time.time() - start_time)}\033[0ms')


class RS_dcs(object):

    def __init__(self, *args, work_env: str = None, auto_harmonised: bool = True, space_optimised: bool = True):

        # init_key_var
        self._sdc_factor_list = []
        self.sparse_matrix, self.huge_matrix = False, False
        self.s2dc_doy_list, self.size_control_factor_list, self.oritif_folder_list = [], [], []
        self.ROI, self.ROI_name, self.ROI_tif, self.ROI_array = None, None, None, None
        self.year_list = []

        # Generate the datacubes list
        self._dcs_backup_ = []
        self._doys_backup_ = []
        self._dc_typelist = []
        self.dcs = []

        # Construct the indicator for different dcs
        self._phemetric_namelist, self._pheyear_list = None, []
        self._withPhemetricdc_, self._withDenvdc_, self._withS2dc_ = False, False, False
        self._s2dc_work_env, self._phemetric_work_env, self._denv_work_env = None, None, None

        # Separate into Denv Phemetric and Sentinel-2 datacube
        for args_temp in args:
            if not isinstance(args_temp, Sentinel2_dc) and not isinstance(args_temp, Denv_dc) and not isinstance(
                    args_temp, Phemetric_dc):
                raise TypeError(
                    'The Sentinel2 datacubes should be a bunch of Sentinel2 datacube, phemetric datacube or Denv datacube!')
            else:
                self._dcs_backup_.append(args_temp)
                if isinstance(args_temp, Phemetric_dc):
                    self._doys_backup_.append(args_temp.paraname_list)
                elif isinstance(args_temp, Sentinel2_dc) or isinstance(args_temp, Denv_dc):
                    self._doys_backup_.append(args_temp.sdc_doylist)
                self._dc_typelist.append(type(args_temp))

        if len(self._dcs_backup_) == 0:
            raise ValueError('Please input at least one valid Sentinel2/Phemetric/Denv datacube')

        if type(auto_harmonised) != bool:
            raise TypeError('Please input the auto harmonised factor as bool type!')
        else:
            harmonised_factor = False

        # Merge all the fundamental factor
        factor_all = ['dc_XSize', 'dc_YSize', 'dc_ZSize']
        for args_temp in args:
            factor_all.extend(list(args_temp._fund_factor))
        factor_all = list(set(factor_all))

        # Input all metadata
        for args_temp in args:
            for factor_temp in factor_all:
                if f'_{factor_temp}_list' not in self.__dict__.keys():
                    self.__dict__[f'_{factor_temp}_list'] = []

                if factor_temp in args_temp.__dict__.keys():
                    self.__dict__[f'_{factor_temp}_list'].append(args_temp.__dict__[factor_temp])
                else:
                    self.__dict__[f'_{factor_temp}_list'].append(None)

        for factor_temp in factor_all:
            if len(self.__dict__[f'_{factor_temp}_list']) != len(self.__dict__[f'_{factor_all[0]}_list']):
                raise ImportError('The factor of some dcs is not properly imported!')

        # Check the consistency of datacube size
        x_size, y_size, z_S2size, z_Phemetric_size, z_Denv_size = 0, 0, 0, 0, 0
        for _ in range(len(self._dc_typelist)):

            if self._dc_typelist[_] == Sentinel2_dc:
                self._withS2dc_ = True
                # Retrieve the shape inform
                if x_size == 0 and y_size == 0:
                    x_size, y_size = self._dcs_backup_[_].dc_XSize, self._dcs_backup_[_].dc_YSize

                if z_S2size == 0:
                    z_S2size = self._dcs_backup_[_].dc_ZSize

                if x_size != self._dcs_backup_[_].dc_XSize or y_size != self._dcs_backup_[_].dc_YSize:
                    raise Exception('Please make sure all the Sentinel-2 datacube share the same size!')
                elif z_S2size != self._dcs_backup_[_].dc_ZSize:
                    if auto_harmonised:
                        harmonised_factor = True
                    else:
                        raise Exception(
                            'The Sentinel-2datacubes is not consistent in the date dimension! Turn auto harmonised factor as True if wanna avoid this problem!')

            elif self._dc_typelist[_] == Denv_dc:
                self._withDenvdc_ = True
                # Retrieve the shape inform
                if x_size == 0 and y_size == 0:
                    x_size, y_size = self._dcs_backup_[_].dc_XSize, self._dcs_backup_[_].dc_YSize

                if x_size != self._dcs_backup_[_].dc_XSize or y_size != self._dcs_backup_[_].dc_YSize:
                    raise Exception('Please make sure all the Denv datacube share the same size!')

            elif self._dc_typelist[_] == Phemetric_dc:
                self._withPhemetricdc_ = True
                # Retrieve the shape inform
                if x_size == 0 and y_size == 0:
                    x_size, y_size = self._dcs_backup_[_].dc_XSize, self._dcs_backup_[_].dc_YSize

                if z_Phemetric_size == 0:
                    z_Phemetric_size = self._dcs_backup_[_].dc_ZSize

                if x_size != self._dcs_backup_[_].dc_XSize or y_size != self._dcs_backup_[_].dc_YSize:
                    raise Exception('Please make sure all the Denv datacube share the same size!')
                elif z_Phemetric_size != self._dcs_backup_[_].dc_ZSize:
                    raise Exception(
                        'The Phemetric_dc datacubes is not consistent in the Z dimension! Double check the input!')

        if x_size != 0 and y_size != 0:
            self.dcs_XSize, self.dcs_YSize = x_size, y_size
        else:
            raise Exception('Error occurred when obtaining the x y size of s2 dcs!')

        # Check the consistency of S2dcs
        if self._withS2dc_:
            s2dc_pos = [i for i, v in enumerate(self._dc_typelist) if v == Sentinel2_dc]
            if len(s2dc_pos) != 1:
                for factor_temp in ['ROI', 'ROI_name', 'ROI_array', 'Datatype', 'ROI_tif', 'coordinate_system',
                                    'sparse_matrix', 'huge_matrix', 'sdc_factor', 'dc_group_list', 'tiles']:
                    if False in [
                        self.__dict__[f'_{factor_temp}_list'][s2dc_pos[0]] == self.__dict__[f'_{factor_temp}_list'][pos]
                        for pos in s2dc_pos]:
                        raise ValueError(f'Please make sure the {factor_temp} for all the dcs were consistent!')

                # Read the doy or date list
                if False in [self._sdc_factor_list[pos_temp] for pos_temp in s2dc_pos]:
                    raise Exception('Please sequenced the Sentinel-2 datacubes before further process!')
                else:
                    if False in [len(self._doys_backup_[s2dc_pos[0]]) == len(self._doys_backup_[pos_temp]) for pos_temp
                                 in s2dc_pos] or False in [
                        (self._doys_backup_[pos_temp] == self._doys_backup_[s2dc_pos[0]]) for pos_temp in s2dc_pos]:
                        if auto_harmonised:
                            harmonised_factor = True
                        else:
                            raise Exception(
                                'The datacubes is not consistent in the date dimension! Turn auto harmonised factor as True if wanna avoid this problem!')
                    else:
                        self.s2dc_doy_list = self._doys_backup_[s2dc_pos[0]]

                # Harmonised the dcs
                if harmonised_factor:
                    self._auto_harmonised_s2dcs()

                # Determine the Zsize
                if z_S2size != 0:
                    self.S2dc_ZSize = z_S2size
                else:
                    raise Exception('Error occurred when obtaining the Z size of s2 dc!')

                # Define the output_path
                if work_env is None:
                    self._s2dc_work_env = Path(
                        os.path.dirname(os.path.dirname(self._dcs_backup_[s2dc_pos[0]].dc_filepath))).path_name
                else:
                    self._s2dc_work_env = work_env

        # Check the consistency of Denv dcs
        if self._withDenvdc_:
            Denvdc_pos = [i for i, v in enumerate(self._dc_typelist) if v == Denv_dc]
            if len(Denvdc_pos) != 1:
                for factor_temp in ['Datatype', 'coordinate_system', 'sparse_matrix', 'huge_matrix', 'sdc_factor',
                                    'dc_group_list', 'tiles']:
                    if False in [
                        self.__dict__[f'_{factor_temp}_list'][Denvdc_pos[0]] == self.__dict__[f'_{factor_temp}_list'][
                            pos] for pos in Denvdc_pos]:
                        raise ValueError(f'Please make sure the {factor_temp} for all the dcs were consistent!')

            # Construct Denv doylist
            self.Denv_doy_list = [None for _ in range(len(self._doys_backup_))]
            for _ in Denvdc_pos:
                self.Denv_doy_list[_] = self._doys_backup_[_]

            # Define the output_path
            if work_env is None:
                self._denv_work_env = Path(
                    os.path.dirname(os.path.dirname(self._dcs_backup_[Denvdc_pos[0]].Denv_dc_filepath))).path_name
            else:
                self._denv_work_env = work_env

            # Determine the denv index
            self.Denv_indexlist = list(
                set([self._index_list[_] for _ in range(len(self._index_list)) if self._dc_typelist[_] == Denv_dc]))

        # Check the consistency of Phemetric dcs
        if self._withPhemetricdc_:
            Phemetricdc_pos = [i for i, v in enumerate(self._dc_typelist) if v == Phemetric_dc]
            if len(Phemetricdc_pos) != 1:
                for factor_temp in ['ROI', 'ROI_name', 'ROI_array', 'Datatype', 'ROI_tif', 'coordinate_system',
                                    'sparse_matrix', 'huge_matrix', 'dc_group_list', 'tiles']:
                    if False in [self.__dict__[f'_{factor_temp}_list'][Phemetricdc_pos[0]] ==
                                 self.__dict__[f'_{factor_temp}_list'][pos] for pos in Phemetricdc_pos]:
                        raise ValueError(f'Please make sure the {factor_temp} for all the dcs were consistent!')

            # Define the output_path
            if work_env is None:
                self._pheme_work_env = Path(os.path.dirname(
                    os.path.dirname(self._dcs_backup_[Phemetricdc_pos[0]].Phemetric_dc_filepath))).path_name
            else:
                self._pheme_work_env = work_env

            # Determine the Zsize
            if z_Phemetric_size != 0:
                self.Phedc_ZSize = z_Phemetric_size
            else:
                raise Exception('Error occurred when obtaining the Z size of s2 dc!')

            # Construct phemetric namelist
            self._phemetric_namelist = []
            for _ in Phemetricdc_pos:
                self._phemetric_namelist.extend(
                    [temp.split(str(self._pheyear_list[_]) + '_')[-1] for temp in self._doys_backup_[_]])
            self._phemetric_namelist = list(set(self._phemetric_namelist))

            pheyear = []
            for _ in self._pheyear_list:
                if _ not in pheyear and _ is not None:
                    pheyear.append(_)
                elif _ is not None:
                    raise ValueError('There are duplicate pheyears for different pheme dcs!')

        # Check consistency between different types of dcs (ROI/Time range)
        # Check the ROI consistency
        if [self._withS2dc_, self._withDenvdc_, self._withPhemetricdc_].count(True) > 1:
            if False in [temp == self._ROI_list[0] for temp in self._ROI_list] or [temp == self._ROI_name_list[0] for
                                                                                   temp in self._ROI_name_list]:
                bounds_list = [bf.raster_ds2bounds(temp) for temp in self._ROI_tif_list]
                crs_list = [gdal.Open(temp).GetProjection() for temp in self._ROI_tif_list]
                if False in [temp == bounds_list[0] for temp in bounds_list] or False in [temp == crs_list[0] for temp
                                                                                          in crs_list]:
                    try:
                        array_list = [np.sum(np.load(temp)) for temp in self._ROI_array_list]
                    except MemoryError:
                        array_list = [len(sm.csr_matrix(np.load(temp)).data) for temp in self._ROI_array_list]
                    if False in [temp == array_list[0] for temp in array_list]:
                        raise Exception('The ROIs between different types of datacube were not consistent')
                    else:
                        if self._withS2dc_:
                            self.ROI, self.ROI_name, self.ROI_tif, self.ROI_array = self._ROI_list[s2dc_pos[0]], \
                            self._ROI_name_list[s2dc_pos[0]], self._ROI_tif_list[s2dc_pos[0]], self._ROI_array_list[
                                s2dc_pos[0]]
                        elif self._withPhemetricdc_:
                            self.ROI, self.ROI_name, self.ROI_tif, self.ROI_array = self._ROI_list[Denvdc_pos[0]], \
                            self._ROI_name_list[Denvdc_pos[0]], self._ROI_tif_list[Denvdc_pos[0]], self._ROI_array_list[
                                Denvdc_pos[0]]
                else:
                    if self._withS2dc_:
                        self.ROI, self.ROI_name, self.ROI_tif, self.ROI_array = self._ROI_list[s2dc_pos[0]], \
                        self._ROI_name_list[s2dc_pos[0]], self._ROI_tif_list[s2dc_pos[0]], self._ROI_array_list[
                            s2dc_pos[0]]
                    elif self._withPhemetricdc_:
                        self.ROI, self.ROI_name, self.ROI_tif, self.ROI_array = self._ROI_list[Denvdc_pos[0]], \
                        self._ROI_name_list[Denvdc_pos[0]], self._ROI_tif_list[Denvdc_pos[0]], self._ROI_array_list[
                            Denvdc_pos[0]]
            else:
                self.ROI, self.ROI_name, self.ROI_tif, self.ROI_array = self._ROI_list[0], self._ROI_name_list[0], \
                self._ROI_tif_list[0], self._ROI_array_list[0]
        else:
            self.ROI, self.ROI_name, self.ROI_tif, self.ROI_array = self._ROI_list[0], self._ROI_name_list[0], \
            self._ROI_tif_list[0], self._ROI_array_list[0]

        # Check the temporal consistency
        if [self._withS2dc_, self._withDenvdc_, self._withPhemetricdc_].count(True) > 1:
            if self._withS2dc_:
                s2dc_year_range = set([int(np.floor(temp / 10000)) for temp in self.s2dc_doy_list])
            else:
                s2dc_year_range = None

            if self._withPhemetricdc_:
                phedc_year_range = [_ for _ in self._pheyear_list if _ is not None]
            else:
                phedc_year_range = None

            if self._withDenvdc_:
                Denvdc_year_range = []
                for _ in Denvdc_pos:
                    Denvdc_year_range.extend(self._doys_backup_[_])
                Denvdc_year_range = set([int(np.floor(_ / 1000)) for _ in Denvdc_year_range])
            else:
                Denvdc_year_range = None

        # Check the matrix consistency
        if [self._withS2dc_, self._withDenvdc_, self._withPhemetricdc_].count(True) > 1:
            if self._withS2dc_:
                s2dc_year_range = set([int(np.floor(temp / 10000)) for temp in self.s2dc_doy_list])
            else:
                s2dc_year_range = None

            if self._withPhemetricdc_:
                phedc_year_range = [_ for _ in self._pheyear_list if _ is not None]
            else:
                phedc_year_range = None

            if self._withDenvdc_:
                Denvdc_year_range = []
                for _ in Denvdc_pos:
                    Denvdc_year_range.extend(self._doys_backup_[_])
                Denvdc_year_range = set([int(np.floor(_ / 1000)) for _ in Denvdc_year_range])
            else:
                Denvdc_year_range = None

        # Construct the datacube list
        for _ in self._dcs_backup_:
            self.dcs.append(copy.copy(_.dc))
            if space_optimised is True:
                _.dc = None

        # Define var for the flood mapping
        self.inun_det_method_dic = {}
        self._variance_num = 2
        self._inundation_overwritten_factor = False
        self._DEM_path = None
        self._DT_std_fig_construction = False
        self._append_inundated_dc = True
        self._flood_mapping_accuracy_evaluation_factor = False
        self._sample_rs_link_list = None
        self._sample_data_path = None
        self._flood_mapping_method = ['Unet', 'MNDWI_thr', 'DT']

        # Define var for the phenological analysis
        self._curve_fitting_algorithm = None
        self._flood_removal_method = None
        self._curve_fitting_dic = {}
        self._curfit_result = None

        # Define var for NIPY reconstruction
        self._add_NIPY_dc = True
        self._NIPY_overwritten_factor = False

        # Define var for phenology metrics generation
        self._phenology_index_all = ['annual_ave_VI', 'flood_ave_VI', 'unflood_ave_VI', 'max_VI', 'max_VI_doy',
                                     'bloom_season_ave_VI', 'well_bloom_season_ave_VI']
        self._curve_fitting_dic = {}
        self._all_quantify_str = None

        # Define var for flood_free_phenology_metrics
        self._flood_free_pm = ['annual_max_VI', 'average_VI_between_max_and_flood']

    def __sizeof__(self):
        size = 0
        for dc in self.dcs:
            size += dc.__sizeof__()
        return size

    def _auto_harmonised_s2dcs(self):

        doy_all = np.array([])
        for doy_temp in self._doys_backup_:
            doy_all = np.concatenate([doy_all, doy_temp], axis=0)
        doy_all = np.sort(np.unique(doy_all))

        i = 0
        while i < len(self._doys_backup_):
            for doy in doy_all:
                if doy not in self._doys_backup_[i]:
                    m_factor = True
                    if not self.sparse_matrix:
                        self.dcs[i] = np.insert(self.dcs[i], np.argwhere(doy_all == doy).flatten()[0],
                                                np.nan * np.zeros([self.dcs_YSize, self.dcs_XSize, 1]), axis=2)
                    else:
                        self.dcs[i].append(self.dcs[i]._matrix_type(np.zeros([self.dcs_YSize, self.dcs_XSize])),
                                           name=int(doy), pos=np.argwhere(doy_all == doy).flatten()[0])
            i += 1

        if False in [doy_all.shape[0] == self.dcs[i].shape[2] for i in range(len(self.dcs))]:
            raise ValueError('The autoharmised is failed')

        self.S2dc_ZSize = len(doy_all)
        self.s2dc_doy_list = doy_all.tolist()

        for t in range(len(self._doys_backup_)):
            self._doys_backup_[t] = self.s2dc_doy_list

        for tt in self._dcs_backup_:
            tt.sdc_doylist = self.s2dc_doy_list
            tt.dc_ZSize = self.S2dc_ZSize

    def append(self, dc_temp: Sentinel2_dc) -> None:
        if type(dc_temp) is not Sentinel2_dc:
            raise TypeError('The appended data should be a Sentinel2_dc!')

        for indicator in ['ROI', 'ROI_name', 'sdc_factor', 'coordinate_system', 'sparse_matrix', 'huge_matrix']:
            if dc_temp.__dict__[indicator] != self.__dict__[indicator]:
                raise ValueError('The appended datacube is not consistent with the original datacubes')

        if self.dcs_XSize != dc_temp.dc_XSize or self.dcs_YSize != dc_temp.dc_YSize or self.S2dc_ZSize != dc_temp.dc_ZSize:
            raise ValueError('The appended datacube has different size compared to the original datacubes')

        if self.s2dc_doy_list != dc_temp.sdc_doylist:
            raise ValueError('The appended datacube has doy list compared to the original datacubes')

        self.index_list.append(dc_temp.index)
        self.oritif_folder_list.append(dc_temp.oritif_folder)
        self.dcs.append(dc_temp.dc)
        self._doys_backup_.append(dc_temp.sdc_doylist)
        self._dcs_backup_.append(dc_temp)
        self._dcs_backup_[-1].dc = None

    def remove(self, index):
        if index not in self.index_list:
            raise ValueError(f'The {index} is not in the index list!')

        num = self.index_list.index(index)
        self.dcs.remove(self.dcs[num])
        self.size_control_factor_list.remove(self.size_control_factor_list[num])
        self.oritif_folder_list.remove(self.oritif_folder_list[num])
        self.index_list.remove(self.index_list[num])

    def extend(self, dcs_temp) -> None:
        if type(dcs_temp) is not RS_dcs:
            raise TypeError('The appended data should be a Sentinel2_dcs!')

        for indicator in ['ROI', 'ROI_name', 'sdc_factor', 'dcs_XSize', 'dcs_YSize', 'dcs_ZSize', 'doy_list',
                          'coordinate_system']:
            if dcs_temp.__dict__[indicator] != self.__dict__[indicator]:
                raise ValueError('The appended datacube is not consistent with the original datacubes')

        self.index_list.extend(dcs_temp.index_list)
        self.oritif_folder.extend(dcs_temp.oritif_folder)
        self._doys_backup_.extend(dcs_temp._doys_backup_)
        self.dcs.extend(dcs_temp.dcs)

    def _process_inun_det_para(self, **kwargs):
        self._append_inundated_dc = True
        self._inundated_ow_para = False

        for key_temp in kwargs.keys():
            if key_temp not in ['append_inundated_dc', 'overwritten_para']:
                raise ValueError(f'The {str(key_temp)} for func inundation detection is not supported!')

            if key_temp == 'append_inundated_dc' and type(kwargs['append_inundated_dc']) is bool:
                self._append_inundated_dc = kwargs['append_inundated_dc']
            elif key_temp == 'overwritten_para' and type(kwargs['overwritten_para']) is bool:
                self._inundated_ow_para = kwargs['overwritten_para']

    def inundation_detection(self, method: str, **kwargs):
        # process inundation detection method
        self._process_inun_det_para(**kwargs)

        start_time = time.time()
        print(f'Start detecting the inundation area in the \033[1;34m{self.ROI_name}\033[0m')

        # Method 1 MNDWI static threshold
        if method not in self._flood_mapping_method:
            raise ValueError(f'The inundation detection method {str(method)} is not supported')

        if method == 'MNDWI_thr':
            if self.size_control_factor_list[self.index_list.index('MNDWI')]:
                MNDWI_static_thr = 33768
            else:
                MNDWI_static_thr = 0.1

            if 'inundation_MNDWI_thr' not in self.index_list or self._inundated_ow_para:
                if not os.path.exists(self._s2dc_work_env + 'inundation_MNDWI_thr_sequenced_datacube\\header.npy'):
                    bf.create_folder(self._s2dc_work_env + 'inundation_MNDWI_thr_sequenced_datacube\\')
                    if 'MNDWI' in self.index_list:
                        if self.sparse_matrix:
                            namelist = self.dcs[self.index_list.index('MNDWI')].SM_namelist
                            inundation_sm = NDSparseMatrix()
                            for z_temp in range(self.S2dc_ZSize):
                                inundation_array = self.dcs[self.index_list.index('MNDWI')].SM_group[namelist[z_temp]]
                                inundation_array.data[inundation_array.data < MNDWI_static_thr] = -1
                                inundation_array.data[inundation_array.data > MNDWI_static_thr] = 0
                                inundation_array.data[inundation_array.data == -1] = 1
                                inundation_sm.append(inundation_array, name=namelist[z_temp])

                            inundation_dc = copy.deepcopy(self._dcs_backup_[self.index_list.index('MNDWI')])
                            inundation_dc.dc = inundation_sm
                            inundation_dc.index = 'inundation_' + method
                            inundation_dc.sdc_doylist = self.s2dc_doy_list
                            inundation_dc.save(self._s2dc_work_env + 'inundation_MNDWI_thr_sequenced_datacube\\')

                        else:
                            inundation_array = copy.deepcopy(self.dcs[self.index_list.index('MNDWI')])
                            inundation_array = inundation_array >= MNDWI_static_thr

                            inundation_dc = copy.deepcopy(self._dcs_backup_[self.index_list.index('MNDWI')])
                            inundation_dc.dc = inundation_array
                            inundation_dc.index = 'inundation_' + method
                            inundation_dc.save(self._s2dc_work_env + 'inundation_MNDWI_thr_sequenced_datacube\\')

                        if self._append_inundated_dc:
                            self.append(inundation_dc)
                            self.remove('MNDWI')
                    else:
                        raise ValueError('Please construct a valid datacube with MNDWI sdc inside!')
                else:
                    inundation_dc = Sentinel2_dc(self._s2dc_work_env + 'inundation_MNDWI_thr_sequenced_datacube\\')
                    self.append(inundation_dc)
                    self.remove('MNDWI')

        print(
            f'Finish detecting the inundation area in the \033[1;34m{self.ROI_name}\033[0m using \033[1;31m{str(time.time() - start_time)}\033[0m s!')

    def _process_inundation_removal_para(self, **kwargs):
        pass

    def inundation_removal(self, processed_index, inundation_method, append_new_dc=True, **kwargs):

        start_time = time.time()

        # process arguments
        inundation_index = inundation_method
        self._process_inundation_removal_para(**kwargs)

        if processed_index not in self.index_list or inundation_index not in self.index_list:
            raise ValueError('The inundation removal or index is not properly generated!')

        inundation_dc = self.dcs[self.index_list.index(inundation_index)]
        processed_dc = copy.deepcopy(self.dcs[self.index_list.index(processed_index)])
        processed_dc4save = copy.deepcopy(self._dcs_backup_[self.index_list.index(processed_index)])

        if not os.path.exists(self._s2dc_work_env + processed_index + '_noninun_sequenced_datacube\\header.npy'):
            bf.create_folder(self._s2dc_work_env + processed_index + '_noninun_sequenced_datacube\\')
            if self.sparse_matrix:

                for height in range(self.S2dc_ZSize):
                    processed_dc.SM_group[processed_dc.SM_namelist[height]] = processed_dc.SM_group[
                        processed_dc.SM_namelist[height]].multiply(
                        inundation_dc.SM_group[inundation_dc.SM_namelist[height]])

                # if self._remove_nan_layer or self._manually_remove_para:
                #     i_temp = 0
                #     while i_temp < len(doy_list):
                #         if data_valid_array[i_temp]:
                #             if doy_list[i_temp] in data_cube.SM_namelist:
                #                 data_cube.remove_layer(doy_list[i_temp])
                #             doy_list.remove(doy_list[i_temp])
                #             data_valid_array = np.delete(data_valid_array, i_temp, 0)
                #             i_temp -= 1
                #         i_temp += 1

                processed_index = processed_index + '_noninun'
                processed_dc4save.index = processed_index
                processed_dc4save.dc = processed_dc
                processed_dc4save.save(self._s2dc_work_env + processed_index + '_sequenced_datacube\\')

                if append_new_dc:
                    self.append(processed_dc4save)
            else:
                pass
        else:
            processed_dc = Sentinel2_dc(self._s2dc_work_env + processed_index + '_noninun_sequenced_datacube\\')
            if append_new_dc:
                self.append(processed_dc)

        print(
            f'Finish remove the inundation area of the \033[1;34m{processed_index}\033[0m using \033[1;31m{str(time.time() - start_time)}\033[0m s!')

    def _process_curve_fitting_para(self, **kwargs):

        # Curve fitting method
        all_supported_curve_fitting_method = ['seven_para_logistic', 'two_term_fourier']

        if 'curve_fitting_algorithm' in kwargs.keys():
            self._curve_fitting_algorithm = kwargs['curve_fitting_algorithm']

        if self._curve_fitting_algorithm is None or self._curve_fitting_algorithm == 'seven_para_logistic':
            self._curve_fitting_dic['CFM'] = 'SPL'
            self._curve_fitting_dic['para_num'] = 7
            self._curve_fitting_dic['initial_para_ori'] = [0.3, 0.5, 108.2, 7.596, 280.4, 7.473, 0.00225]
            self._curve_fitting_dic['initial_para_boundary'] = (
                [0.2, 0.2, 40, 3, 180, 3, 0.001], [0.6, 0.8, 180, 17, 330, 17, 0.01])
            self._curve_fitting_dic['para_ori'] = [0.10, 0.8802, 108.2, 7.596, 311.4, 7.473, 0.00225]
            self._curve_fitting_dic['para_boundary'] = (
                [0.08, 0.0, 50, 6.2, 285, 4.5, 0.0015], [0.20, 0.8, 130, 11.5, 350, 8.8, 0.0028])
            self._curve_fitting_algorithm = seven_para_logistic_function
        elif self._curve_fitting_algorithm == 'two_term_fourier':
            self._curve_fitting_dic['CFM'] = 'TTF'
            self._curve_fitting_dic['para_num'] = 6
            self._curve_fitting_dic['para_ori'] = [0, 0, 0, 0, 0, 0.017]
            self._curve_fitting_dic['para_boundary'] = (
                [0, -0.5, -0.5, -0.05, -0.05, 0.015], [1, 0.5, 0.5, 0.05, 0.05, 0.019])
            self._curve_fitting_algorithm = two_term_fourier
        elif self._curve_fitting_algorithm not in all_supported_curve_fitting_method:
            ValueError(f'The curve fitting method {self._curve_fitting_algorithm} is not supported!')

        # Determine inundation removal method
        if 'flood_removal_method' in kwargs.keys():
            self._flood_removal_method = kwargs['flood_removal_method']

            if self._flood_removal_method not in self._flood_mapping_method:
                raise ValueError(f'The flood removal method {self._flood_removal_method} is not supported!')
        else:
            self._flood_removal_method = None

        # Overwritten_para
        if 'overwritten' in kwargs.keys():
            self._curve_fitting_algorithm = kwargs['curve_fitting_algorithm']

    def curve_fitting(self, index, **kwargs):

        # Check vi availability
        if index not in self.index_list:
            raise ValueError('Please make sure the vi datacube is constructed!')

        # Process paras
        self._process_curve_fitting_para(**kwargs)

        # Get the index/doy dc
        index_dc = copy.copy(self.dcs[self.index_list.index(index)])
        doy_dc = copy.copy(self.s2dc_doy_list)
        doy_all = np.mod(doy_dc, 1000)
        size_control_fac = self.size_control_factor_list[self.index_list.index(index)]

        # Define the study region
        sa_map = np.load(self.ROI_array)
        ds_temp = gdal.Open(self.ROI_tif)

        # Create output path
        curfit_output_path = self._s2dc_work_env + index + '_curfit_datacube\\'
        para_output_path = curfit_output_path + str(self._curve_fitting_dic['CFM']) + '_para\\'
        phemetric_output_path = self._s2dc_work_env + index + f'_curfit_datacube\\{self.ROI_name}_Phemetric_datacube\\'
        csv_para_output_path = para_output_path + 'csv_file\\'
        tif_para_output_path = para_output_path + 'tif_file\\'
        bf.create_folder(curfit_output_path)
        bf.create_folder(para_output_path)
        bf.create_folder(phemetric_output_path)
        bf.create_folder(csv_para_output_path)
        bf.create_folder(tif_para_output_path)
        self._curve_fitting_dic[
            str(self.ROI) + '_' + str(index) + '_' + str(self._curve_fitting_dic['CFM']) + '_path'] = para_output_path

        # Define the cache folder
        cache_folder = f'{para_output_path}cache\\'
        bf.create_folder(cache_folder)

        # Read pos_df
        if not os.path.exists(f'{cache_folder}pos_df.csv'):
            pos_df = pd.DataFrame(np.argwhere(sa_map != -32768), columns=['y', 'x'])
            pos_df = pos_df.sort_values(['x', 'y'], ascending=[True, True])
            pos_df = pos_df.reset_index()
        else:
            pos_df = pd.read_csv(f'{cache_folder}pos_df.csv')

        # Generate all the curve fitting para into a table
        if not os.path.exists(csv_para_output_path + 'curfit_all.csv'):

            if self.huge_matrix:

                # Slice into several tasks/blocks to use all cores
                work_num = int(os.cpu_count() / 2)
                doy_all_list, pos_list, xy_offset_list, index_size_list, index_dc_list, indi_size = [], [], [], [], [], int(
                    np.ceil(pos_df.shape[0] / work_num))
                for i_size in range(work_num):
                    if i_size != work_num - 1:
                        pos_list.append(pos_df[indi_size * i_size: indi_size * (i_size + 1)])
                    else:
                        pos_list.append(pos_df[indi_size * i_size: -1])

                    index_size_list.append(
                        [int(max(0, pos_list[-1]['y'].min())), int(min(sa_map.shape[0], pos_list[-1]['y'].max())),
                         int(max(0, pos_list[-1]['x'].min())), int(min(sa_map.shape[1], pos_list[-1]['x'].max()))])
                    xy_offset_list.append([int(max(0, pos_list[-1]['y'].min())), int(max(0, pos_list[-1]['x'].min()))])

                    if self.sparse_matrix:
                        dc_temp = index_dc.extract_matrix(([index_size_list[-1][0], index_size_list[-1][1]],
                                                           [index_size_list[-1][2], index_size_list[-1][3]], ['all']))
                        index_dc_list.append(dc_temp.drop_nanlayer())
                        doy_all_list.append(bf.date2doy(index_dc_list[-1].SM_namelist))
                    else:
                        index_dc_list.append(index_dc[index_size_list[-1][0]: index_size_list[-1][2],
                                             index_size_list[-1][1]: index_size_list[-1][3], :])

                with concurrent.futures.ProcessPoolExecutor(max_workers=work_num) as executor:
                    result = executor.map(curfit4bound_annual, pos_list, index_dc_list, doy_all_list,
                                          repeat(self._curve_fitting_dic), repeat(self.sparse_matrix),
                                          repeat(size_control_fac), xy_offset_list, repeat(cache_folder))
                result_list = list(result)

            else:
                result_list = []
                result_list.append(
                    curfit4bound_annual(pos_df, index_dc, doy_all, self._curve_fitting_dic, self.sparse_matrix,
                                        size_control_fac, [0, 0], cache_folder))

            # Integrate all the result into the para dict
            self._curfit_result = None
            for result_temp in result_list:
                if self._curfit_result is None:
                    self._curfit_result = copy.copy(result_temp)
                else:
                    self._curfit_result = pd.concat([self._curfit_result, result_temp])

            key_list = []
            for key_temp in self._curfit_result.keys():
                if key_temp not in pos_df.keys() and 'para' in key_temp:
                    pos_df.insert(len(pos_df.keys()), key_temp, np.nan)
                    key_list.append(key_temp)

            self._curfit_result.to_csv(csv_para_output_path + 'curfit_all.csv')
        else:
            self._curfit_result = pd.read_csv(csv_para_output_path + 'curfit_all.csv')

        # Create output key list
        key_list = []
        df_list = []
        for key_temp in self._curfit_result.keys():
            if True not in [nr_key in key_temp for nr_key in
                            ['Unnamed', 'level', 'index', 'Rsquare']] and key_temp != 'y' and key_temp != 'x':
                key_list.append(key_temp)
                df_list.append(self._curfit_result.loc[:, ['y', 'x', key_temp]])

        # Create tif file based on phenological parameter
        for _ in range(len(key_list)):
            if not os.path.exists(tif_para_output_path + key_list[_] + '.TIF'):
                arr_result = curfit_pd2raster(df_list[_], key_list[_], ds_temp.RasterYSize, ds_temp.RasterXSize)
                bf.write_raster(ds_temp, arr_result[1], tif_para_output_path, arr_result[0] + '.TIF',
                                raster_datatype=gdal.GDT_Float32, nodatavalue=np.nan)

        # Create Phemetric dc
        year_list = set([int(np.floor(temp / 10000)) for temp in self.s2dc_doy_list])

        metadata_dic = {'ROI_name': self.ROI_name, 'index': index, 'Datatype': 'float', 'ROI': self.ROI,
                        'ROI_array': self.ROI_array, 'ROI_tif': self.ROI_tif, 'Phemetric_factor': True,
                        'coordinate_system': self.coordinate_system, 'size_control_factor': False,
                        'oritif_folder': tif_para_output_path, 'dc_group_list': None, 'tiles': None,
                        'curfit_dic': self._curve_fitting_dic}

        with concurrent.futures.ProcessPoolExecutor(max_workers=2) as executor:
            executor.map(cf2phemetric_dc, repeat(tif_para_output_path), repeat(phemetric_output_path), year_list,
                         repeat(index), repeat(metadata_dic))

    def _process_phenology_metrics_para(self, **kwargs):

        self._curve_fitting_algorithm = None
        if 'curve_fitting_algorithm' in kwargs.keys():
            self._curve_fitting_algorithm = kwargs['curve_fitting_algorithm']

        # Curve fitting method
        all_supported_curve_fitting_method = ['seven_para_logistic', 'two_term_fourier']
        self._curve_fitting_dic = {}
        if self._curve_fitting_algorithm is None or self._curve_fitting_algorithm == 'seven_para_logistic':
            self._curve_fitting_dic['CFM'] = 'SPL'
            self._curve_fitting_dic['para_num'] = 7
            self._curve_fitting_algorithm = seven_para_logistic_function
        elif self._curve_fitting_algorithm == 'two_term_fourier':
            self._curve_fitting_dic['CFM'] = 'TTF'
            self._curve_fitting_dic['para_num'] = 6
            self._curve_fitting_algorithm = two_term_fourier
        elif self._curve_fitting_algorithm not in all_supported_curve_fitting_method:
            print('Please double check the curve fitting method')
            sys.exit(-1)

    def phenology_metrics_generation(self, index_list, phenology_index, **kwargs):

        # Check the VI method
        if type(index_list) is str and index_list in self.index_list:
            index_list = [index_list]
        elif type(index_list) is list and False not in [VI_temp in self.index_list for VI_temp in index_list]:
            pass
        else:
            raise TypeError(
                f'The input VI {index_list} was not in supported type (list or str) or some input VI is not in the Landsat_dcs!')

        # Detect the para
        self._process_phenology_metrics_para(**kwargs)

        # Determine the phenology metrics extraction method
        if phenology_index is None:
            phenology_index = ['annual_ave_VI']
        elif type(phenology_index) == str:
            if phenology_index in self._phenology_index_all:
                phenology_index = [phenology_index]
            elif phenology_index not in self._phenology_index_all:
                raise NameError(f'{phenology_index} is not supported!')
        elif type(phenology_index) == list:
            for phenology_index_temp in phenology_index:
                if phenology_index_temp not in self._phenology_index_all:
                    phenology_index.remove(phenology_index_temp)
            if len(phenology_index) == 0:
                print('Please choose the correct phenology index!')
                sys.exit(-1)
        else:
            print('Please choose the correct phenology index!')
            sys.exit(-1)

        sa_map = np.load(self.ROI_array, allow_pickle=True)
        for index_temp in index_list:

            # input the cf dic
            input_annual_file = self._s2dc_work_env + index_temp + '_curfit_datacube\\' + self._curve_fitting_dic[
                'CFM'] + '\\annual_cf_para.npy'
            input_year_file = self._s2dc_work_env + index_temp + '_curfit_datacube\\' + self._curve_fitting_dic[
                'CFM'] + '\\year.npy'
            if not os.path.exists(input_annual_file) or not os.path.exists(input_year_file):
                raise Exception('Please generate the cf para before the generation of phenology metrics')
            else:
                cf_para_dc = np.load(input_annual_file, allow_pickle=True).item()
                year_list = np.load(input_year_file)

            phenology_metrics_inform_dic = {}
            root_output_folder = self._s2dc_work_env + index_temp + '_phenology_metrics\\' + str(
                self._curve_fitting_dic['CFM']) + '\\'
            bf.create_folder(root_output_folder)
            for phenology_index_temp in phenology_index:
                phenology_metrics_inform_dic[phenology_index_temp + '_' + index_temp + '_' + str(
                    self._curve_fitting_dic['CFM']) + '_path'] = root_output_folder + phenology_index_temp + '\\'
                phenology_metrics_inform_dic[phenology_index_temp + '_' + index_temp + '_' + str(
                    self._curve_fitting_dic['CFM']) + '_year'] = year_list
                bf.create_folder(phenology_metrics_inform_dic[phenology_index_temp + '_' + index_temp + '_' + str(
                    self._curve_fitting_dic['CFM']) + '_path'])

            # Main procedure
            doy_temp = np.linspace(1, 365, 365)
            for year in year_list:
                year = int(year)
                annual_para = cf_para_dc[str(year) + '_cf_para']
                if not os.path.exists(root_output_folder + 'annual\\' + str(year) + '_phe_metrics.npy'):
                    annual_phe = np.zeros([annual_para.shape[0], annual_para.shape[1], 365])

                    for y_temp in range(annual_para.shape[0]):
                        for x_temp in range(annual_para.shape[1]):
                            if sa_map[y_temp, x_temp] == -32768:
                                annual_phe[y_temp, x_temp, :] = np.nan
                            else:
                                if self._curve_fitting_dic['para_num'] == 7:
                                    annual_phe[y_temp, x_temp, :] = self._curve_fitting_algorithm(
                                        doy_temp, annual_para[y_temp, x_temp, 0], annual_para[y_temp, x_temp, 1],
                                        annual_para[y_temp, x_temp, 2], annual_para[y_temp, x_temp, 3],
                                        annual_para[y_temp, x_temp, 4], annual_para[y_temp, x_temp, 5],
                                        annual_para[y_temp, x_temp, 6]).reshape([1, 1, 365])
                                elif self._curve_fitting_dic['para_num'] == 6:
                                    annual_phe[y_temp, x_temp, :] = self._curve_fitting_algorithm(
                                        doy_temp, annual_para[y_temp, x_temp, 0], annual_para[y_temp, x_temp, 1],
                                        annual_para[y_temp, x_temp, 2], annual_para[y_temp, x_temp, 3],
                                        annual_para[y_temp, x_temp, 4], annual_para[y_temp, x_temp, 5]).reshape(
                                        [1, 1, 365])

                    bf.create_folder(root_output_folder + 'annual\\')
                    np.save(root_output_folder + 'annual\\' + str(year) + '_phe_metrics.npy', annual_phe)
                else:
                    annual_phe = np.load(root_output_folder + 'annual\\' + str(year) + '_phe_metrics.npy')

                # Generate the phenology metrics
                for phenology_index_temp in phenology_index:
                    phe_metrics = np.zeros([self.dcs_YSize, self.dcs_XSize])
                    phe_metrics[sa_map == -32768] = np.nan

                    if not os.path.exists(
                            phenology_metrics_inform_dic[phenology_index_temp + '_' + index_temp + '_' + str(
                                    self._curve_fitting_dic['CFM']) + '_path'] + str(year) + '_phe_metrics.TIF'):
                        if phenology_index_temp == 'annual_ave_VI':
                            phe_metrics = np.mean(annual_phe, axis=2)
                        elif phenology_index_temp == 'flood_ave_VI':
                            phe_metrics = np.mean(annual_phe[:, :, 182: 302], axis=2)
                        elif phenology_index_temp == 'unflood_ave_VI':
                            phe_metrics = np.mean(
                                np.concatenate((annual_phe[:, :, 0:181], annual_phe[:, :, 302:364]), axis=2), axis=2)
                        elif phenology_index_temp == 'max_VI':
                            phe_metrics = np.max(annual_phe, axis=2)
                        elif phenology_index_temp == 'max_VI_doy':
                            phe_metrics = np.argmax(annual_phe, axis=2) + 1
                        elif phenology_index_temp == 'bloom_season_ave_VI':
                            phe_temp = copy.copy(annual_phe)
                            phe_temp[phe_temp < 0.3] = np.nan
                            phe_metrics = np.nanmean(phe_temp, axis=2)
                        elif phenology_index_temp == 'well_bloom_season_ave_VI':
                            phe_temp = copy.copy(annual_phe)
                            max_index = np.argmax(annual_phe, axis=2)
                            for y_temp_temp in range(phe_temp.shape[0]):
                                for x_temp_temp in range(phe_temp.shape[1]):
                                    phe_temp[y_temp_temp, x_temp_temp, 0: max_index[y_temp_temp, x_temp_temp]] = np.nan
                            phe_temp[phe_temp < 0.3] = np.nan
                            phe_metrics = np.nanmean(phe_temp, axis=2)
                        phe_metrics = phe_metrics.astype(np.float)
                        phe_metrics[sa_map == -32768] = np.nan
                        write_raster(gdal.Open(self.ROI_tif), phe_metrics, phenology_metrics_inform_dic[
                            phenology_index_temp + '_' + index_temp + '_' + str(
                                self._curve_fitting_dic['CFM']) + '_path'],
                                     str(year) + '_phe_metrics.TIF', raster_datatype=gdal.GDT_Float32)
            np.save(self._s2dc_work_env + index_temp + '_phenology_metrics\\' + str(
                self._curve_fitting_dic['CFM']) + '_phenology_metrics.npy', phenology_metrics_inform_dic)

    def generate_phenology_metric(self, **kwargs):
        pass

    def _process_link_GEDI_temp_DPAR(self, **kwargs):
        # Detect whether all the indicators are valid
        for kwarg_indicator in kwargs.keys():
            if kwarg_indicator not in ['accumulated_method', 'static_thr', 'phemetric_window']:
                raise NameError(f'{kwarg_indicator} is not supported kwargs! Please double check!')

        # process accumulated_method
        if 'accumulated_method' in kwargs.keys():
            if isinstance(kwargs['accumulated_method'], str) and kwargs['accumulated_method'] in ['static_thr',
                                                                                                  'phemetric_thr']:
                self._GEDI_link_S2_retrieval_method = kwargs['accumulated_method']
            else:
                raise TypeError('Please mention the dc_overwritten_para should be str type!')
        else:
            self._GEDI_link_S2_retrieval_method = 'phemetric_thr'

        if 'static_thr' in kwargs.keys():
            if isinstance(kwargs['static_thr'], (int, float, complex)):
                self._link_GEDI_denv_method = ['static_thr', kwargs['static_thr']]
            else:
                raise TypeError('Please mention the static_thr should be a number!')
        elif self._GEDI_link_S2_retrieval_method == 'static_thr':
            self._link_GEDI_denv_method = ['phemetric_thr', 10]

        if 'phemetric_window' in kwargs.keys():
            if isinstance(kwargs['phemetric_window'], int):
                self._link_GEDI_denv_method = ['phemetric_thr', kwargs['phemetric_window']]
            else:
                raise TypeError('Please mention the phemetric_window should be int type!')
        else:
            self._link_GEDI_denv_method = ['phemetric_thr', 10]

    def link_GEDI_accumulated_Denv(self, GEDI_xlsx_file, denv_list, **kwargs):

        # Process para
        self._process_link_GEDI_temp_DPAR(**kwargs)
        for _ in denv_list:
            if _ not in self.Denv_indexlist:
                raise TypeError(f'The {str(_)} is not imported into the Sentinel2 dcs')

        # Retrieve the S2 inform
        raster_gt = gdal.Open(self.ROI_tif).GetGeoTransform()
        raster_proj = retrieve_srs(gdal.Open(self.ROI_tif))

        # Retrieve GEDI inform
        GEDI_list = gedi.GEDI_list(GEDI_xlsx_file)
        GEDI_list.reprojection(raster_proj, xycolumn_start='EPSG')

        # Construct Denv list
        for denv_temp in denv_list:
            if not os.path.exists(GEDI_xlsx_file.split('.xlsx')[0] + f'_accumulated_{denv_temp}.csv'):

                # Divide the GEDI and dc into different blocks
                block_amount = os.cpu_count()
                indi_block_size = int(np.ceil(GEDI_list.df_size / block_amount))

                # Allocate the GEDI_list and dc
                GEDI_list_blocked, denvdc_blocked, raster_gt_list, doy_list_integrated = [], [], [], []

                # Phe dc count and pos
                denvdc_count = len([_ for _ in self._index_list if _ == denv_temp])
                denvdc_pos = [_ for _ in range(len(self._index_list)) if self._index_list[_] == denv_temp]

                # Reconstruct the phenology dc
                denvdc_reconstructed = None
                for _ in range(denvdc_count):
                    if denvdc_reconstructed is None:
                        if self._sparse_matrix_list[denvdc_pos[_]]:
                            denvdc_reconstructed = self.dcs[denvdc_pos[_]]
                        else:
                            denvdc_reconstructed = self.dcs[denvdc_pos[_]]
                    else:
                        if self._sparse_matrix_list[denvdc_pos[_]]:
                            denvdc_reconstructed.extend_layers(self.dcs[denvdc_pos[_]])
                        else:
                            denvdc_reconstructed = np.concatenate((denvdc_reconstructed, self.dcs[denvdc_pos[_]]),
                                                                  axis=2)
                    doy_list_integrated.extend(self._doys_backup_[denvdc_pos[_]])

                for i in range(block_amount):
                    if i != block_amount - 1:
                        GEDI_list_blocked.append(GEDI_list.GEDI_df[i * indi_block_size: (i + 1) * indi_block_size])
                    else:
                        GEDI_list_blocked.append(GEDI_list.GEDI_df[i * indi_block_size: -1])

                    ymin_temp, ymax_temp, xmin_temp, xmax_temp = GEDI_list_blocked[-1].EPSG_lat.max() + 12.5, \
                                                                 GEDI_list_blocked[-1].EPSG_lat.min() - 12.5, \
                                                                 GEDI_list_blocked[-1].EPSG_lon.min() - 12.5, \
                                                                 GEDI_list_blocked[-1].EPSG_lon.max() + 12.5
                    cube_ymin, cube_ymax = int(max(0, np.floor((ymin_temp - raster_gt[3]) / raster_gt[5]))), int(
                        min(self.dcs_YSize, np.ceil((ymax_temp - raster_gt[3]) / raster_gt[5])))
                    cube_xmin, cube_xmax = int(max(0, np.floor((xmin_temp - raster_gt[0]) / raster_gt[1]))), int(
                        min(self.dcs_XSize, np.ceil((xmax_temp - raster_gt[0]) / raster_gt[1])))

                    raster_gt_list.append([raster_gt[0] + cube_xmin * raster_gt[1], raster_gt[1], raster_gt[2],
                                           raster_gt[3] + cube_ymin * raster_gt[5], raster_gt[4], raster_gt[5]])

                    if isinstance(denvdc_reconstructed, NDSparseMatrix):
                        sm_temp = denvdc_reconstructed.extract_matrix(
                            ([cube_ymin, cube_ymax + 1], [cube_xmin, cube_xmax + 1], ['all']))
                        denvdc_blocked.append(sm_temp)
                    else:
                        denvdc_blocked.append(
                            denvdc_reconstructed[cube_ymin:cube_ymax + 1, cube_xmin: cube_xmax + 1, :])

                try:
                    # Sequenced code for debug
                    # for i in range(block_amount):
                    #     result = link_GEDI_inform(dc_blocked[i], GEDI_list_blocked[i], bf.date2doy(self.doy_list), raster_gt, 'EPSG', index_temp, 'linear_interpolation', self.size_control_factor_list[self.index_list.index(index_temp)])
                    with concurrent.futures.ProcessPoolExecutor(max_workers=block_amount) as executor:
                        result = executor.map(link_GEDI_accdenvinform, denvdc_blocked, GEDI_list_blocked,
                                              repeat(doy_list_integrated), raster_gt_list, repeat('EPSG'),
                                              repeat(denv_temp))
                except:
                    raise Exception('The s2pheme-GEDI link procedure was interrupted by unknown error!')

                try:
                    result = list(result)
                    gedi_list_output = None

                    for result_temp in result:
                        if gedi_list_output is None:
                            gedi_list_output = copy.copy(result_temp)
                        else:
                            gedi_list_output = pd.concat([gedi_list_output, result_temp])

                    gedi_list_output.to_csv(GEDI_xlsx_file.split('.xlsx')[0] + f'_accumulated_{denv_temp}.csv')
                except:
                    raise Exception('The df output procedure was interrupted by error!')

    def _process_link_GEDI_S2_phenology_para(self, **kwargs):
        # Detect whether all the indicators are valid
        for kwarg_indicator in kwargs.keys():
            if kwarg_indicator not in []:
                raise NameError(f'{kwarg_indicator} is not supported kwargs! Please double check!')

    def link_GEDI_S2_phenology_inform(self, GEDI_xlsx_file, phemetric_list, **kwargs):

        # Process para
        self._process_link_GEDI_S2_para(**kwargs)

        # Retrieve the S2 inform
        raster_gt = gdal.Open(self.ROI_tif).GetGeoTransform()
        raster_proj = retrieve_srs(gdal.Open(self.ROI_tif))

        # Retrieve GEDI inform
        GEDI_list = gedi.GEDI_list(GEDI_xlsx_file)
        GEDI_list.reprojection(raster_proj, xycolumn_start='EPSG')

        # Construct phemetric list
        phemetric_gedi_list = []
        for phemetric_temp in phemetric_list:
            if not os.path.exists(GEDI_xlsx_file.split('.')[0] + f'_{phemetric_temp}.csv'):
                if phemetric_temp not in self._phemetric_namelist:
                    raise Exception(f'The {str(phemetric_temp)} is not a valid index or is not inputted into the dcs!')

                # Divide the GEDI and dc into different blocks
                block_amount = os.cpu_count()
                indi_block_size = int(np.ceil(GEDI_list.df_size / block_amount))

                # Allocate the GEDI_list and dc
                GEDI_list_blocked, phedc_blocked, raster_gt_list, year_list_temp = [], [], [], []

                # Phe dc count and pos
                phedc_count = len([_ for _ in self._pheyear_list if _ is not None])
                phepos = [self._pheyear_list.index(_) for _ in self._pheyear_list if _ is not None]

                # Reconstruct the phenology dc
                phedc_reconstructed = None
                for _ in range(phedc_count):
                    if phedc_reconstructed is None:
                        if self._sparse_matrix_list[phepos[_]]:
                            phedc_reconstructed = NDSparseMatrix(
                                self.dcs[phepos[_]].SM_group[f'{str(self._pheyear_list[phepos[_]])}_{phemetric_temp}'],
                                SM_namelist=[f'{str(self._pheyear_list[phepos[_]])}_{phemetric_temp}'])
                        else:
                            phedc_reconstructed = self.dcs[phepos[_]][:, :, [self._doys_backup_[phepos[_]].index(
                                [f'{str(self._pheyear_list[phepos[_]])}_{phemetric_temp}'])]]
                    else:
                        if self._sparse_matrix_list[phepos[_]]:
                            phedc_reconstructed.add_layer(
                                self.dcs[phepos[_]].SM_group[f'{str(self._pheyear_list[phepos[_]])}_{phemetric_temp}'],
                                f'{str(self._pheyear_list[phepos[_]])}_{phemetric_temp}',
                                phedc_reconstructed.shape[2] + 1)
                        else:
                            phedc_reconstructed = np.concatenate((phedc_reconstructed, self.dcs[phepos[_]][:, :, [
                                                                                                                     self._doys_backup_[
                                                                                                                         phepos[
                                                                                                                             _]].index(
                                                                                                                         [
                                                                                                                             f'{str(self._pheyear_list[phepos[_]])}_{phemetric_temp}'])]]),
                                                                 axis=2)
                    year_list_temp.append(self._pheyear_list[phepos[_]])

                for i in range(block_amount):
                    if i != block_amount - 1:
                        GEDI_list_blocked.append(GEDI_list.GEDI_df[i * indi_block_size: (i + 1) * indi_block_size])
                    else:
                        GEDI_list_blocked.append(GEDI_list.GEDI_df[i * indi_block_size: -1])

                    ymin_temp, ymax_temp, xmin_temp, xmax_temp = GEDI_list_blocked[-1].EPSG_lat.max() + 12.5, \
                                                                 GEDI_list_blocked[-1].EPSG_lat.min() - 12.5, \
                                                                 GEDI_list_blocked[-1].EPSG_lon.min() - 12.5, \
                                                                 GEDI_list_blocked[-1].EPSG_lon.max() + 12.5
                    cube_ymin, cube_ymax = int(max(0, np.floor((ymin_temp - raster_gt[3]) / raster_gt[5]))), int(
                        min(self.dcs_YSize, np.ceil((ymax_temp - raster_gt[3]) / raster_gt[5])))
                    cube_xmin, cube_xmax = int(max(0, np.floor((xmin_temp - raster_gt[0]) / raster_gt[1]))), int(
                        min(self.dcs_XSize, np.ceil((xmax_temp - raster_gt[0]) / raster_gt[1])))

                    raster_gt_list.append([raster_gt[0] + cube_xmin * raster_gt[1], raster_gt[1], raster_gt[2],
                                           raster_gt[3] + cube_ymin * raster_gt[5], raster_gt[4], raster_gt[5]])

                    if isinstance(phedc_reconstructed, NDSparseMatrix):
                        sm_temp = phedc_reconstructed.extract_matrix(
                            ([cube_ymin, cube_ymax + 1], [cube_xmin, cube_xmax + 1], ['all']))
                        phedc_blocked.append(sm_temp.drop_nanlayer())
                    else:
                        phedc_blocked.append(phedc_reconstructed[cube_ymin:cube_ymax + 1, cube_xmin: cube_xmax + 1, :])

                try:
                    # Sequenced code for debug
                    # for i in range(block_amount):
                    #     result = link_GEDI_inform(dc_blocked[i], GEDI_list_blocked[i], bf.date2doy(self.doy_list), raster_gt, 'EPSG', index_temp, 'linear_interpolation', self.size_control_factor_list[self.index_list.index(index_temp)])
                    with concurrent.futures.ProcessPoolExecutor(max_workers=block_amount) as executor:
                        result = executor.map(link_GEDI_pheinform, phedc_blocked, GEDI_list_blocked,
                                              repeat(year_list_temp), raster_gt_list, repeat('EPSG'),
                                              repeat(phemetric_temp))
                except:
                    raise Exception('The s2pheme-GEDI link procedure was interrupted by unknown error!')

                try:
                    result = list(result)
                    gedi_list_output = None

                    for result_temp in result:
                        if gedi_list_output is None:
                            gedi_list_output = copy.copy(result_temp)
                        else:
                            gedi_list_output = pd.concat([gedi_list_output, result_temp])

                    gedi_list_output.to_csv(GEDI_xlsx_file.split('.')[0] + f'_{phemetric_temp}.csv')
                except:
                    raise Exception('The df output procedure was interrupted by error!')
            phemetric_gedi_list.append(pd.read_csv(GEDI_xlsx_file.split('.')[0] + f'_{phemetric_temp}.csv'))

        # Output to a single file
        if not os.path.exists(GEDI_xlsx_file.split('.')[0] + f'_all_Phemetrics.csv'):
            i = 0
            phemetric_output = None
            for phemetric_temp in phemetric_list:
                phe_gedilist_temp = phemetric_gedi_list[i].sort_values('Unnamed: 0')
                if phemetric_output is None:
                    phemetric_output = phe_gedilist_temp
                else:
                    key_temp = [_ for _ in list(phe_gedilist_temp.keys()) if phemetric_temp in _][0]
                    phemetric_output.insert(phemetric_output.shape[1], key_temp, phe_gedilist_temp[key_temp])
                i += 1
            phemetric_output.to_csv(GEDI_xlsx_file.split('.')[0] + f'_all_Phemetrics.csv')

    def _process_link_GEDI_S2_para(self, **kwargs):

        # Detect whether all the indicators are valid
        for kwarg_indicator in kwargs.keys():
            if kwarg_indicator != 'retrieval_method':
                raise NameError(f'{kwarg_indicator} is not supported kwargs! Please double check!')

        # process clipped_overwritten_para
        if 'retrieval_method' in kwargs.keys():
            if type(kwargs['retrieval_method']) is str and kwargs['retrieval_method'] in ['nearest_neighbor',
                                                                                          'linear_interpolation']:
                self._GEDI_link_S2_retrieval_method = kwargs['retrieval_method']
            else:
                raise TypeError('Please mention the dc_overwritten_para should be str type!')
        else:
            self._GEDI_link_S2_retrieval_method = 'nearest_neighbor'

    def link_GEDI_S2_inform(self, GEDI_xlsx_file, index_list, **kwargs):

        # Two different method Nearest neighbor and linear interpolation
        self._process_link_GEDI_S2_para(**kwargs)

        # Retrieve the S2 inform
        raster_gt = gdal.Open(self.ROI_tif).GetGeoTransform()
        raster_proj = retrieve_srs(gdal.Open(self.ROI_tif))

        # Retrieve GEDI inform
        GEDI_list = gedi.GEDI_list(GEDI_xlsx_file)
        GEDI_list.reprojection(raster_proj, xycolumn_start='EPSG')

        for index_temp in index_list:

            if index_temp not in self._index_list:
                raise Exception(f'The {str(index_temp)} is not a valid index or is not inputted into the dcs!')

            # Divide the GEDI and dc into different blocks
            block_amount = os.cpu_count()
            indi_block_size = int(np.ceil(GEDI_list.df_size / block_amount))

            # Allocate the GEDI_list and dc
            GEDI_list_blocked, dc_blocked, raster_gt_list, doy_list_temp = [], [], [], []
            for i in range(block_amount):
                if i != block_amount - 1:
                    GEDI_list_blocked.append(GEDI_list.GEDI_df[i * indi_block_size: (i + 1) * indi_block_size])
                else:
                    GEDI_list_blocked.append(GEDI_list.GEDI_df[i * indi_block_size: -1])

                ymin_temp, ymax_temp, xmin_temp, xmax_temp = GEDI_list_blocked[-1].EPSG_lat.max() + 12.5, \
                                                             GEDI_list_blocked[-1].EPSG_lat.min() - 12.5, \
                                                             GEDI_list_blocked[-1].EPSG_lon.min() - 12.5, \
                                                             GEDI_list_blocked[-1].EPSG_lon.max() + 12.5
                cube_ymin, cube_ymax, cube_xmin, cube_xmax = int(
                    max(0, np.floor((ymin_temp - raster_gt[3]) / raster_gt[5]))), int(
                    min(self.dcs_YSize, np.ceil((ymax_temp - raster_gt[3]) / raster_gt[5]))), int(
                    max(0, np.floor((xmin_temp - raster_gt[0]) / raster_gt[1]))), int(
                    min(self.dcs_XSize, np.ceil((xmax_temp - raster_gt[0]) / raster_gt[1])))

                if isinstance(self.dcs[self._index_list.index(index_temp)], NDSparseMatrix):
                    sm_temp = self.dcs[self._index_list.index(index_temp)].extract_matrix(
                        ([cube_ymin, cube_ymax + 1], [cube_xmin, cube_xmax + 1], ['all']))
                    dc_blocked.append(sm_temp.drop_nanlayer())
                    doy_list_temp.append(bf.date2doy(dc_blocked[-1].SM_namelist))
                elif isinstance(self.dcs[self._index_list.index(index_temp)], np.ndarray):
                    dc_blocked.append(
                        self.dcs[self._index_list.index(index_temp)][cube_ymin:cube_ymax + 1, cube_xmin: cube_xmax + 1,
                        :])
                    doy_list_temp.append(bf.date2doy(self.s2dc_doy_list))
                raster_gt_list.append([raster_gt[0] + cube_xmin * raster_gt[1], raster_gt[1], raster_gt[2],
                                       raster_gt[3] + cube_ymin * raster_gt[5], raster_gt[4], raster_gt[5]])

            try:
                # Sequenced code for debug
                # for i in range(block_amount):
                #     result = link_GEDI_inform(dc_blocked[i], GEDI_list_blocked[i], bf.date2doy(self.doy_list), raster_gt, 'EPSG', index_temp, 'linear_interpolation', self.size_control_factor_list[self.index_list.index(index_temp)])
                with concurrent.futures.ProcessPoolExecutor(max_workers=block_amount) as executor:
                    result = executor.map(link_GEDI_inform, dc_blocked, GEDI_list_blocked, doy_list_temp,
                                          raster_gt_list, repeat('EPSG'), repeat(index_temp),
                                          repeat('linear_interpolation'),
                                          repeat(self._size_control_factor_list[self._index_list.index(index_temp)]))
            except:
                raise Exception('The link procedure was interrupted by error!')

            try:
                result = list(result)
                index_combined_name = '_'
                index_combined_name = index_combined_name.join(index_list)
                gedi_list_output = None

                for result_temp in result:
                    if gedi_list_output is None:
                        gedi_list_output = copy.copy(result_temp)
                    else:
                        gedi_list_output = pd.concat([gedi_list_output, result_temp])

                gedi_list_output.to_csv(GEDI_xlsx_file.split('.')[0] + f'_{index_combined_name}.csv')
            except:
                raise Exception('The df output procedure was interrupted by error!')

    def process_denv_via_pheme(self, denvname, phename):

        if denvname not in self.Denv_indexlist:
            raise ValueError(f'The denv index {str(denvname)} is not imported')

        if phename not in self._phemetric_namelist:
            raise ValueError(f'The denv index {str(phename)} is not imported')

        if phename == 'SOS':
            # Phe dc count and pos
            denvdc_count = len([_ for _ in self._index_list if _ == denvname])
            denvdc_pos = [_ for _ in range(len(self._index_list)) if self._index_list[_] == denvname]

            pheme_reconstructed = None
            pheme_namelist = []
            # Reconstruct the phenology dc
            for _ in range(denvdc_count):
                denvdc_year = list(set([int(np.floor(_ / 1000)) for _ in self.Denv_doy_list[denvdc_pos[_]]]))

                for year_temp in denvdc_year:
                    if year_temp not in self._pheyear_list:
                        raise TypeError(f'The phemetric of {str(year_temp)} is not imported')
                    else:
                        phepos = self._pheyear_list.index(year_temp)
                        if f'{str(year_temp)}_static_{denvname}' not in self._doys_backup_[phepos]:
                            try:
                                if pheme_reconstructed is None or (isinstance(pheme_reconstructed,
                                                                              NDSparseMatrix) and f'{str(self._pheyear_list[phepos])}_SOS' not in pheme_reconstructed.SM_namelist) or (
                                        isinstance(pheme_reconstructed,
                                                   np.ndarray) and f'{str(self._pheyear_list[phepos])}_SOS' not in pheme_namelist):
                                    if pheme_reconstructed is None:
                                        if self._sparse_matrix_list[phepos]:
                                            pheme_reconstructed = NDSparseMatrix(
                                                self.dcs[phepos].SM_group[f'{str(self._pheyear_list[phepos])}_SOS'],
                                                SM_namelist=[f'{str(self._pheyear_list[phepos])}_SOS'])
                                        else:
                                            pheme_reconstructed = self.dcs[phepos][:, :, [self._doys_backup_[
                                                                                              phepos].index(
                                                [f'{str(self._pheyear_list[phepos])}_SOS'])]]
                                            pheme_namelist.append(f'{str(self._pheyear_list[phepos])}_SOS')
                                    else:
                                        if self._sparse_matrix_list[phepos]:
                                            pheme_reconstructed.add_layer(
                                                self.dcs[phepos].SM_group[f'{str(self._pheyear_list[phepos])}_SOS'],
                                                f'{str(self._pheyear_list[phepos])}_SOS',
                                                pheme_reconstructed.shape[2] + 1)
                                        else:
                                            pheme_reconstructed = np.concatenate((pheme_reconstructed,
                                                                                  self.dcs[phepos][:, :, [
                                                                                                             self._doys_backup_[
                                                                                                                 phepos].index(
                                                                                                                 [
                                                                                                                     f'{str(self._pheyear_list[phepos])}_SOS'])]]),
                                                                                 axis=2)
                                            pheme_namelist.append(f'{str(self._pheyear_list[phepos])}_SOS')

                                if pheme_reconstructed is None or (isinstance(pheme_reconstructed,
                                                                              NDSparseMatrix) and f'{str(self._pheyear_list[phepos])}_peak_doy' not in pheme_reconstructed.SM_namelist) or (
                                        isinstance(pheme_reconstructed,
                                                   np.ndarray) and f'{str(self._pheyear_list[phepos])}_peak_doy' not in pheme_namelist):
                                    if pheme_reconstructed is None:
                                        if self._sparse_matrix_list[phepos]:
                                            pheme_reconstructed = NDSparseMatrix(self.dcs[phepos].SM_group[
                                                                                     f'{str(self._pheyear_list[phepos])}_peak_doy'],
                                                                                 SM_namelist=[
                                                                                     f'{str(self._pheyear_list[phepos])}_peak_doy'])
                                        else:
                                            pheme_reconstructed = self.dcs[phepos][:, :, [self._doys_backup_[
                                                                                              phepos].index(
                                                [f'{str(self._pheyear_list[phepos])}_peak_doy'])]]
                                            pheme_namelist.append(f'{str(self._pheyear_list[phepos])}_peak_doy')
                                    else:
                                        if self._sparse_matrix_list[phepos]:
                                            pheme_reconstructed.add_layer(self.dcs[phepos].SM_group[
                                                                              f'{str(self._pheyear_list[phepos])}_peak_doy'],
                                                                          f'{str(self._pheyear_list[phepos])}_peak_doy',
                                                                          pheme_reconstructed.shape[2] + 1)
                                        else:
                                            pheme_reconstructed = np.concatenate((pheme_reconstructed,
                                                                                  self.dcs[phepos][:, :, [
                                                                                                             self._doys_backup_[
                                                                                                                 phepos].index(
                                                                                                                 [
                                                                                                                     f'{str(self._pheyear_list[phepos])}_peak_doy'])]]),
                                                                                 axis=2)
                                        pheme_namelist.append(f'{str(self._pheyear_list[phepos])}_peak_doy')

                            except:
                                raise Exception('SOS or peak doy is not properly retrieved!')

                            # Retrieve the phemetric inform
                            if isinstance(pheme_reconstructed, NDSparseMatrix):
                                sos = np.round(pheme_reconstructed.SM_group[f'{str(year_temp)}_SOS'].toarray())
                                peak_doy = np.round(
                                    pheme_reconstructed.SM_group[f'{str(year_temp)}_peak_doy'].toarray())
                            elif isinstance(pheme_reconstructed, np.ndarray):
                                sos = np.round(
                                    pheme_reconstructed[:, :, pheme_namelist.index(f'{str(year_temp)}_SOS')].resshape(
                                        [pheme_reconstructed.shape[0], pheme_reconstructed.shape[1]]))
                                peak_doy = np.round(pheme_reconstructed[:, :,
                                                    pheme_namelist.index(f'{str(year_temp)}_peak_doy')].resshape(
                                    [pheme_reconstructed.shape[0], pheme_reconstructed.shape[1]]))
                            else:
                                raise TypeError('The para phemetric dc is not imported as a supported datatype!')

                            base_env = copy.copy(sos)
                            base_env[base_env <= 0] = 0
                            base_env[base_env != 0] = -1

                            sos = sos + year_temp * 1000
                            sos[sos <= year_temp * 1000] = 3000000
                            sos = sos.astype(np.int32)

                            peak_doy = peak_doy + year_temp * 1000
                            peak_doy[peak_doy <= year_temp * 1000] = 0
                            peak_doy = peak_doy.astype(np.int32)

                            year_doy = self._doys_backup_[denvdc_pos[_]]

                            # Create static/base env map
                            xy_all = np.argwhere(base_env == -1)
                            xy_all = pd.DataFrame(xy_all, columns=['y', 'x'])
                            xy_all = xy_all.sort_values(['x', 'y'])
                            y_all, x_all = list(xy_all['y']), list(xy_all['x'])

                            block_amount = os.cpu_count()
                            indi_block_size = int(np.ceil(len(x_all) / block_amount))

                            # Allocate the GEDI_list and dc
                            y_all_blocked, x_all_blocked, denv_dc_blocked, xy_offset_blocked, sos_blocked = [], [], [], [], []
                            for i in range(block_amount):
                                if i != block_amount - 1:
                                    y_all_blocked.append(y_all[i * indi_block_size: (i + 1) * indi_block_size])
                                    x_all_blocked.append(x_all[i * indi_block_size: (i + 1) * indi_block_size])
                                else:
                                    y_all_blocked.append(y_all[i * indi_block_size:])
                                    x_all_blocked.append(x_all[i * indi_block_size:])

                                if isinstance(self.dcs[denvdc_pos[_]], NDSparseMatrix):
                                    denv_dc_blocked.append(self.dcs[denvdc_pos[_]].extract_matrix(([min(
                                        y_all_blocked[-1]), max(y_all_blocked[-1]) + 1], [min(x_all_blocked[-1]),
                                                                                          max(x_all_blocked[-1]) + 1],
                                                                                                   ['all'])))
                                else:
                                    pass

                                sos_blocked.append(sos[min(y_all_blocked[-1]): max(y_all_blocked[-1]) + 1,
                                                   min(x_all_blocked[-1]): max(x_all_blocked[-1]) + 1])
                                xy_offset_blocked.append([min(y_all_blocked[-1]), min(x_all_blocked[-1])])

                            with concurrent.futures.ProcessPoolExecutor() as exe:
                                result = exe.map(get_base_denv, y_all_blocked, x_all_blocked, sos_blocked,
                                                 repeat(year_doy), denv_dc_blocked, xy_offset_blocked)

                            result = list(result)
                            for result_temp in result:
                                for r_ in result_temp:
                                    base_env[r_[0], r_[1]] = r_[2]
                            base_env[base_env == -1] = 0
                            base_env[np.isnan(base_env)] = 0
                            self._dcs_backup_[phepos].dc = copy.copy(self.dcs[phepos])
                            self._dcs_backup_[phepos]._add_layer(
                                type(self.dcs[phepos].SM_group[f'{str(year_temp)}_SOS'])(base_env),
                                f'{str(year_temp)}_static_{denvname}')
                            self._dcs_backup_[phepos].save(self._dcs_backup_[phepos].Phemetric_dc_filepath)
                            self._dcs_backup_[phepos].dc = None
                        else:
                            # Retrieve the phemetric inform
                            if isinstance(self.dcs[phepos], NDSparseMatrix):
                                sos = np.round(self.dcs[phepos].SM_group[f'{str(year_temp)}_SOS'].toarray())
                                peak_doy = np.round(self.dcs[phepos].SM_group[f'{str(year_temp)}_peak_doy'].toarray())
                                base_env = self.dcs[phepos].SM_group[f'{str(year_temp)}_static_{denvname}'].toarray()
                            elif isinstance(self.dcs[phepos], np.ndarray):
                                sos = np.round(
                                    self.dcs[phepos][:, :, pheme_namelist.index(f'{str(year_temp)}_SOS')].reshape(
                                        [self.dcs[phepos].shape[0], self.dcs[phepos].shape[1]]))
                                peak_doy = np.round(
                                    self.dcs[phepos][:, :, pheme_namelist.index(f'{str(year_temp)}_peak_doy')].reshape(
                                        [self.dcs[phepos].shape[0], self.dcs[phepos].shape[1]]))
                                base_env = self.dcs[phepos][:, :,
                                           pheme_namelist.index(f'{str(year_temp)}_static_{denvname}')].reshape(
                                    [self.dcs[phepos].shape[0], self.dcs[phepos].shape[1]])
                            else:
                                raise TypeError('The para phemetric dc is not imported as a supported datatype!')

                            sos = sos + year_temp * 1000
                            sos[sos <= year_temp * 1000] = 3000000
                            sos = sos.astype(np.int32)

                            peak_doy = peak_doy + year_temp * 1000
                            peak_doy[peak_doy <= year_temp * 1000] = 0
                            peak_doy = peak_doy.astype(np.int32)

                        peak_doy_env = copy.copy(peak_doy)
                        peak_doy_env = peak_doy_env.astype(float)
                        peak_doy_env[peak_doy_env != 0] = 0

                        for doy in self._doys_backup_[denvdc_pos[_]]:
                            sos_temp = sos <= doy
                            sos_temp = sos_temp.astype(int)
                            peak_doy_temp = peak_doy >= doy
                            peak_doy_temp = peak_doy_temp.astype(float)
                            if isinstance(self.dcs[denvdc_pos[_]], NDSparseMatrix):
                                temp = (self.dcs[denvdc_pos[_]].SM_group[doy] - self.dcs[denvdc_pos[_]]._matrix_type(
                                    base_env)).multiply(self.dcs[denvdc_pos[_]]._matrix_type(sos_temp))
                                temp[temp < 0] = 0
                                self.dcs[denvdc_pos[_]].SM_group[doy] = type(temp)(
                                    temp.astype(self.dcs[denvdc_pos[_]].SM_group[doy].dtype).toarray())
                                peak_doy_env += self.dcs[denvdc_pos[_]].SM_group[doy].toarray() * peak_doy_temp
                            else:
                                self.dcs[denvdc_pos[_]][:, :, self._doys_backup_[denvdc_pos[_]].index(doy)] = (self.dcs[
                                                                                                                   denvdc_pos[
                                                                                                                       _]][
                                                                                                               :, :,
                                                                                                               self._doys_backup_[
                                                                                                                   denvdc_pos[
                                                                                                                       _]].index(
                                                                                                                   doy)] - base_env) * sos_temp
                                peak_doy_env += self.dcs[denvdc_pos[_]][:, :,
                                                self._doys_backup_[denvdc_pos[_]].index(doy)] * peak_doy_temp
                        self._dcs_backup_[phepos].dc = copy.copy(self.dcs[phepos])
                        self._dcs_backup_[phepos]._add_layer(
                            type(self.dcs[phepos].SM_group[f'{str(year_temp)}_SOS'])(peak_doy_env),
                            f'{str(year_temp)}_peak_{denvname}')
                        self._dcs_backup_[phepos].save(self._dcs_backup_[phepos].Phemetric_dc_filepath)
                        self._dcs_backup_[phepos].dc = None

                self._dcs_backup_[denvdc_pos[_]].dc = copy.copy(self.dcs[denvdc_pos[_]])
                ori_index, ori_path = self._dcs_backup_[denvdc_pos[_]].index, self._dcs_backup_[
                    denvdc_pos[_]].Denv_dc_filepath
                self._dcs_backup_[denvdc_pos[_]].index, self._dcs_backup_[
                    denvdc_pos[_]].Denv_dc_filepath = ori_index + '_relative', os.path.dirname(
                    os.path.dirname(ori_path)) + '\\' + ori_path.split('\\')[-2] + '_relative\\'
                self._dcs_backup_[denvdc_pos[_]].save(self._dcs_backup_[denvdc_pos[_]].Denv_dc_filepath)
                self._dcs_backup_[denvdc_pos[_]].dc = None
                self._dcs_backup_[denvdc_pos[_]].index, self._dcs_backup_[
                    denvdc_pos[_]].Denv_dc_filepath = ori_index, ori_path
        else:
            pass

    def create_feature_list_by_date(self, date: list, index: str, output_folder: str):

        # Determine the date
        date_pro, peak_factor, year_pro = [], False, []
        for _ in date:
            if isinstance(_, int) and _ > 10000000:
                date_pro.append(bf.date2doy(_))
                year_pro.append(_ // 10000)
            elif isinstance(_, int) and _ > 1000000:
                date_pro.append(_)
                year_pro.append(_ // 1000)
            elif isinstance(_, str) and _.startswith('peak'):
                date_pro.append(_)
                year_t = None

                for q in range(0, len(_) - 3):
                    try:
                        year_t = int(_[q: q + 4])
                    except:
                        pass

                if year_t is None:
                    raise TypeError('The peak type should follow the year!')
                elif year_t is not None and year_t not in self._pheyear_list:
                    raise TypeError(f'The phemetric of {str(year_t)} is not input!')
                elif self._phemetric_namelist is not None and 'peak_doy' not in self._phemetric_namelist:
                    raise TypeError(f'The peak doy of {str(year_t)} is not generated!')
                else:
                    peak_factor = True
                    year_pro.append(year_t)
            else:
                raise TypeError('The date was not supported!')

            # Create output folder
            if isinstance(output_folder, str):
                output_folder = Path(output_folder).path_name
                bf.create_folder(output_folder) if not os.path.exists(output_folder) else None
                bf.create_folder(output_folder + str(_) + '\\') if not os.path.exists(
                    output_folder + str(_) + '\\') else None
            else:
                raise TypeError('The output folder should be a string!')

        # Generate the pos xy list
        roi_ds = gdal.Open(self._ROI_tif_list[0])
        roi_map = roi_ds.GetRasterBand(1).ReadAsArray()
        xy_all = np.argwhere(roi_map != roi_ds.GetRasterBand(1).GetNoDataValue()) if ~np.isnan(
            roi_ds.GetRasterBand(1).GetNoDataValue()) else np.argwhere(~np.isnan(roi_map))
        xy_all = pd.DataFrame(xy_all, columns=['y', 'x'])
        xy_all = xy_all.sort_values(['x', 'y'])

        # Generate the peak doy list
        if peak_factor:

            for q in date_pro:
                if isinstance(q, str) and q.startswith('peak'):

                    year_temp = None
                    for yy in range(0, len(q) - 3):
                        try:
                            year_temp = int(q[yy: yy + 4])
                        except:
                            pass

                    if isinstance(self.dcs[self._pheyear_list.index(year_temp)], NDSparseMatrix):
                        pheme_array = self.dcs[self._pheyear_list.index(year_temp)].SM_group[
                            f'{str(year_temp)}_peak_doy'].toarray()
                    else:
                        pheme_array = self.dcs[self._pheyear_list.index(year_temp)][:, :,
                                      self._doys_backup_[self._pheyear_list.index(year_temp)].index(
                                          f'{str(year_temp)}_peak_doy')]

                    pheme_array[pheme_array == 0] = np.nan
                    pheme_array = pheme_array + year_temp * 1000
                    y_all, x_all = np.mgrid[:pheme_array.shape[0], :pheme_array.shape[1]]
                    arr_out = np.column_stack((y_all.ravel(), x_all.ravel(), pheme_array.ravel()))
                    df_temp = pd.DataFrame(arr_out, columns=['y', 'x', q])
                    xy_all = pd.merge(xy_all, df_temp, on=['x', 'y'], how='left')

        # Itr through index
        for _ in index:

            if _ not in self._index_list and _ not in self._phemetric_namelist:
                raise TypeError(f'The {_} is not input or avaliable !')

            elif _ in self._index_list and self._dc_typelist[self._index_list.index(_)] == Sentinel2_dc:

                # Allocate the GEDI_list and dc
                mod_factor = 's2dc'
                y_all_blocked, x_all_blocked, dc_blocked, xy_offset_blocked, doy_list_temp, req_day_list = [], [], [], [], [], []
                block_amount = os.cpu_count()
                for date_temp in date_pro:

                    if not os.path.exists(f'{output_folder}{str(date_temp)}\\{_}_index.csv'):
                        y_all, x_all = list(xy_all['y']), list(xy_all['x'])
                        peak_doy_all = list(xy_all[date_temp]) if isinstance(date_temp, str) else None
                        indi_block_size = int(np.ceil(len(y_all) / block_amount))

                        if req_day_list == []:
                            req_day_list = [[] for tt in range(block_amount)]

                        for i in range(block_amount):
                            if i != block_amount - 1:
                                if len(y_all_blocked) != block_amount:
                                    y_all_blocked.append(y_all[i * indi_block_size: (i + 1) * indi_block_size])
                                    x_all_blocked.append(x_all[i * indi_block_size: (i + 1) * indi_block_size])
                                elif len(y_all_blocked) != len(x_all_blocked):
                                    raise Exception('Code Error in create feature list')

                                if isinstance(date_temp, str):
                                    req_day_list[i].append(peak_doy_all[i * indi_block_size: (i + 1) * indi_block_size])
                                else:
                                    req_day_list[i].append(
                                        [date_temp for tt in range(i * indi_block_size, (i + 1) * indi_block_size)])

                            else:
                                if len(y_all_blocked) != block_amount:
                                    y_all_blocked.append(y_all[i * indi_block_size:])
                                    x_all_blocked.append(x_all[i * indi_block_size:])
                                elif len(y_all_blocked) != len(x_all_blocked):
                                    raise Exception('Code Error in create feature list')

                                if isinstance(date_temp, str):
                                    req_day_list[i].append(peak_doy_all[i * indi_block_size: (i + 1) * indi_block_size])
                                else:
                                    req_day_list[i].append(
                                        [date_temp for tt in range(i * indi_block_size, (i + 1) * indi_block_size)])

                            if len(dc_blocked) != block_amount and len(doy_list_temp) != block_amount:
                                if isinstance(self.dcs[self._index_list.index(_)], NDSparseMatrix):
                                    sm_temp = self.dcs[self._index_list.index(_)].extract_matrix((
                                                                                                 [min(y_all_blocked[i]),
                                                                                                  max(y_all_blocked[
                                                                                                          i]) + 1],
                                                                                                 [min(x_all_blocked[i]),
                                                                                                  max(x_all_blocked[
                                                                                                          i]) + 1],
                                                                                                 ['all']))
                                    dc_blocked.append(sm_temp)
                                    doy_list_temp.append(bf.date2doy(dc_blocked[i].SM_namelist))
                                elif isinstance(self.dcs[self._index_list.index(_)], np.ndarray):
                                    dc_blocked.append(self.dcs[self._index_list.index(_)][
                                                      y_all_blocked[i].min():y_all_blocked[i].max() + 1,
                                                      x_all_blocked[i].min(): x_all_blocked[i].max() + 1, :])
                                    doy_list_temp.append(bf.date2doy(self.s2dc_doy_list))
                            elif len(dc_blocked) != len(doy_list_temp):
                                raise Exception('Code Error in create feature list')

                            if len(xy_offset_blocked) != block_amount:
                                xy_offset_blocked.append([min(y_all_blocked[i]), min(x_all_blocked[i])])

                with concurrent.futures.ProcessPoolExecutor(max_workers=block_amount) as executor:
                    result = executor.map(get_index_by_date, dc_blocked, y_all_blocked, x_all_blocked, doy_list_temp,
                                          req_day_list, xy_offset_blocked, repeat(_), repeat(date_pro), repeat('index'))

                pd_out = None
                result = list(result)
                for tt in result:
                    if pd_out is None:
                        pd_out = pd.DataFrame(tt)
                    else:
                        pd_out = pd.concat([pd_out, pd.DataFrame(tt)])

            elif self._phemetric_namelist is not None and _ in self._phemetric_namelist:

                # Allocate the GEDI_list and dc
                mod_factor = 'phedc'
                y_all_blocked, x_all_blocked, dc_blocked, xy_offset_blocked, doy_list_temp = [], [], [], [], []
                y_all, x_all = list(xy_all['y']), list(xy_all['x'])
                phedc_reconstructed = None
                block_amount = os.cpu_count()
                indi_block_size = int(np.ceil(len(y_all) / block_amount))

                year_indi = list(set(year_pro))
                for year_t in year_indi:

                    if year_t not in self._pheyear_list:
                        raise ValueError(f'The phemetric under {str(year_t)} is not imported!')
                    else:

                        # Reconstruct the phenology dc
                        phepos = self._pheyear_list.index(year_t)
                        if phedc_reconstructed is None:
                            if isinstance(self.dcs[phepos], NDSparseMatrix):
                                phedc_reconstructed = NDSparseMatrix(
                                    self.dcs[phepos].SM_group[f'{str(self._pheyear_list[phepos])}_{_}'],
                                    SM_namelist=[f'{str(self._pheyear_list[phepos])}_{_}'])
                            else:
                                phedc_reconstructed = self.dcs[phepos][:, :, [self._doys_backup_[phepos].index(
                                    [f'{str(self._pheyear_list[phepos])}_{_}'])]]
                        else:
                            if isinstance(self.dcs[phepos], NDSparseMatrix):
                                phedc_reconstructed = phedc_reconstructed.append(
                                    self.dcs[phepos].SM_group[f'{str(self._pheyear_list[phepos])}_{_}'],
                                    name=[f'{str(self._pheyear_list[phepos])}_{_}'])
                            else:
                                phedc_reconstructed = np.concatenate((phedc_reconstructed, self.dcs[phepos][:, :, [
                                                                                                                      self._doys_backup_[
                                                                                                                          phepos].index(
                                                                                                                          [
                                                                                                                              f'{str(self._pheyear_list[phepos])}_{_}'])]]),
                                                                     axis=2)

                for i in range(block_amount):
                    if i != block_amount - 1:
                        y_all_blocked.append(y_all[i * indi_block_size: (i + 1) * indi_block_size])
                        x_all_blocked.append(x_all[i * indi_block_size: (i + 1) * indi_block_size])
                    else:
                        y_all_blocked.append(y_all[i * indi_block_size:])
                        x_all_blocked.append(x_all[i * indi_block_size:])

                    if isinstance(phedc_reconstructed, NDSparseMatrix):
                        sm_temp = phedc_reconstructed.extract_matrix(([min(y_all_blocked[-1]),
                                                                       max(y_all_blocked[-1]) + 1],
                                                                      [min(x_all_blocked[-1]),
                                                                       max(x_all_blocked[-1]) + 1], ['all']))
                        dc_blocked.append(sm_temp)
                        doy_list_temp.append(year_t)

                    elif isinstance(self.dcs[self._index_list.index(_)], np.ndarray):
                        dc_blocked.append(
                            self.dcs[self._index_list.index(_)][y_all_blocked[-1].min(): y_all_blocked[-1].max() + 1,
                            x_all_blocked[-1].min(): x_all_blocked[-1].max() + 1, :])
                        doy_list_temp.append(year_t)

                    xy_offset_blocked.append([min(y_all_blocked[-1]), min(x_all_blocked[-1])])

                with concurrent.futures.ProcessPoolExecutor(max_workers=block_amount) as executor:
                    result = executor.map(get_index_by_date, dc_blocked, y_all_blocked, x_all_blocked, doy_list_temp,
                                          repeat(year_indi), xy_offset_blocked, repeat(_), repeat(year_indi),
                                          repeat('pheno'))

                pd_out = None
                result = list(result)
                for tt in result:
                    if pd_out is None:
                        pd_out = pd.DataFrame(tt)
                    else:
                        pd_out = pd.concat([pd_out, pd.DataFrame(tt)])

            elif _ in self._index_list and self._dc_typelist[self._index_list.index(_)] == Denv_dc:

                # Allocate the GEDI_list and dc
                mod_factor = 'denvdc'
                y_all_blocked, x_all_blocked, dc_blocked, xy_offset_blocked, doy_list_temp = [], [], [], [], []
                y_all, x_all = list(xy_all['y']), list(xy_all['x'])
                block_amount = os.cpu_count()
                indi_block_size = int(np.ceil(len(y_all) / block_amount))

                # Year_range
                year_indi = list(set(year_pro))
                denv_year_all = []
                for doy_list_t in self.Denv_doy_list:
                    if doy_list_t is not None:
                        doy_list_t = [doy_tt // 1000 for doy_tt in doy_list_t]
                        denv_year_all.extend(list(set(doy_list_t)))
                denv_year_all = list(set(denv_year_all))
                if False in [year_indi_t in denv_year_all for year_indi_t in year_indi]:
                    raise ValueError(f'The denvdc of some years is not imported')

                # Denv dc count and pos
                denvdc_count = len([q for q in self._index_list if q == _])
                denvdc_pos = [q for q in range(len(self._index_list)) if self._index_list[q] == _]

                # Reconstruct the denv dc
                denvdc_reconstructed = None
                for q in range(denvdc_count):
                    if denvdc_reconstructed is None:
                        if self._sparse_matrix_list[denvdc_pos[q]]:
                            denvdc_reconstructed = self.dcs[denvdc_pos[q]]
                        else:
                            denvdc_reconstructed = self.dcs[denvdc_pos[q]]
                    else:
                        if self._sparse_matrix_list[denvdc_pos[q]]:
                            denvdc_reconstructed.extend_layers(self.dcs[denvdc_pos[q]])
                        else:
                            denvdc_reconstructed = np.concatenate((denvdc_reconstructed, self.dcs[denvdc_pos[q]]),
                                                                  axis=2)
                    doy_list_temp.extend(self._doys_backup_[denvdc_pos[q]])

                # Accumulate the denv
                array_temp, reconstructed_doy_list = None, []
                for date_temp in date_pro:
                    if isinstance(date_temp, str) and date_temp.startswith('peak'):
                        if not os.path.exists(f"{output_folder}{str(date_temp)}\\peak_{_.split('_')[0]}_index.csv"):
                            raise Exception(f'Please generate the peak {_}')
                        elif not os.path.exists(
                                f"{output_folder}{str(date_temp)}\\accumulated_{_.split('_')[0]}_index.csv"):
                            shutil.copyfile(f"{output_folder}{str(date_temp)}\\peak_{_.split('_')[0]}_index.csv",
                                            f"{output_folder}{str(date_temp)}\\accumulated_{_.split('_')[0]}_index.csv")
                    else:
                        if not os.path.exists(
                                f"{output_folder}{str(bf.doy2date(date_temp))}\\accumulated_{_.split('_')[0]}_index.csv"):
                            year_temp = int(np.floor(date_temp / 1000))
                            doy_temp = np.mod(date_temp, 1000)
                            doy_templist = range(year_temp * 1000 + 1, year_temp * 1000 + doy_temp + 1)
                            doy_pos = []

                            for q in doy_templist:
                                doy_pos.append(doy_list_temp.index(q))

                            if len(doy_pos) != max(doy_pos) - min(doy_pos) + 1:
                                raise Exception('The doy list is not continuous!')

                            if array_temp is None:
                                if isinstance(denvdc_reconstructed, NDSparseMatrix):
                                    array_temp = denvdc_reconstructed.extract_matrix(
                                        (['all'], ['all'], [min(doy_pos), max(doy_pos) + 1])).sum(axis=2,
                                                                                                  new_layer_name=date_temp)
                                else:
                                    array_temp = np.nansun(denvdc_reconstructed[:, :, min(doy_pos): max(doy_pos) + 1],
                                                           axis=2)
                            else:
                                if isinstance(denvdc_reconstructed, NDSparseMatrix):
                                    array_temp.extend_layers(denvdc_reconstructed.extract_matrix(
                                        (['all'], ['all'], [min(doy_pos), max(doy_pos) + 1])).sum(axis=2,
                                                                                                  new_layer_name=date_temp))
                                else:
                                    array_temp = np.concatenate((array_temp, np.nansun(
                                        denvdc_reconstructed[:, :, min(doy_pos): max(doy_pos) + 1], axis=2)), axis=2)
                            reconstructed_doy_list.append(date_temp)

                req_day_list = [[] for tt in range(block_amount)]
                for i in range(block_amount):
                    if i != block_amount - 1:
                        if len(y_all_blocked) != block_amount:
                            y_all_blocked.append(y_all[i * indi_block_size: (i + 1) * indi_block_size])
                            x_all_blocked.append(x_all[i * indi_block_size: (i + 1) * indi_block_size])
                        elif len(y_all_blocked) != len(x_all_blocked):
                            raise Exception('Code Error in create feature list')
                    else:
                        if len(y_all_blocked) != block_amount:
                            y_all_blocked.append(y_all[i * indi_block_size:])
                            x_all_blocked.append(x_all[i * indi_block_size:])
                        elif len(y_all_blocked) != len(x_all_blocked):
                            raise Exception('Code Error in create feature list')

                    if len(dc_blocked) != block_amount:
                        if isinstance(array_temp, NDSparseMatrix):
                            sm_temp = array_temp.extract_matrix(([min(y_all_blocked[i]), max(y_all_blocked[i]) + 1],
                                                                 [min(x_all_blocked[i]), max(x_all_blocked[i]) + 1],
                                                                 ['all']))
                            dc_blocked.append(sm_temp)
                        elif isinstance(array_temp, np.ndarray):
                            dc_blocked.append(array_temp[min(y_all_blocked[-1]):max(y_all_blocked[-1]) + 1,
                                              min(x_all_blocked[-1]): max(x_all_blocked[-1]) + 1, :])

                    if len(xy_offset_blocked) != block_amount:
                        xy_offset_blocked.append([min(y_all_blocked[i]), min(x_all_blocked[i])])
                    req_day_list[i] = reconstructed_doy_list
                denvdc_reconstructed = None

                with concurrent.futures.ProcessPoolExecutor(max_workers=block_amount) as executor:
                    result = executor.map(get_index_by_date, dc_blocked, y_all_blocked, x_all_blocked, req_day_list,
                                          req_day_list, xy_offset_blocked, repeat(_), req_day_list, repeat('denv'))

                pd_out = None
                result = list(result)
                for tt in result:
                    if pd_out is None:
                        pd_out = pd.DataFrame(tt)
                    else:
                        pd_out = pd.concat([pd_out, pd.DataFrame(tt)])

            if mod_factor == 's2dc':
                for date_temp in date_pro:
                    xy_temp = pd.DataFrame(pd_out, columns=['y', 'x', bf.doy2date(date_temp)]) if not isinstance(
                        date_temp, str) else pd.DataFrame(pd_out, columns=['y', 'x', date_temp])
                    xy_temp.to_csv(f'{output_folder}{str(bf.doy2date(date_temp))}\\{_}_index.csv') if not isinstance(
                        date_temp, str) else xy_temp.to_csv(f'{output_folder}{str(date_temp)}\\{_}_index.csv')
            elif mod_factor == 'phedc':
                for date_temp in date_pro:
                    xy_temp = pd.DataFrame(pd_out, columns=['y', 'x', year_pro[date_pro.index(date_temp)]])
                    xy_temp.to_csv(f'{output_folder}{str(bf.doy2date(date_temp))}\\{_}_index.csv') if not isinstance(
                        date_temp, str) else xy_temp.to_csv(f'{output_folder}{str(date_temp)}\\{_}_index.csv')
            elif mod_factor == 'denvdc':
                for date_temp in date_pro:
                    xy_temp = pd.DataFrame(pd_out, columns=['y', 'x', bf.doy2date(date_temp)]) if not isinstance(
                        date_temp, str) else None
                    xy_temp.to_csv(
                        f"{output_folder}{str(bf.doy2date(date_temp))}\\accumulated_{_.split('_')[0]}_index.csv") if not isinstance(
                        date_temp, str) else None

    def feature_table2tiffile(self, table: str, index: str, outputfolder: str):

        if table.endswith('.xlsx'):
            df_temp = pd.read_excel(table)
            name = table.split('\\')[-1].split('.xlsx')[0]
        elif table.endswith('.csv'):
            df_temp = pd.read_csv(table)
            name = table.split('\\')[-1].split('.csv')[0]
        else:
            raise TypeError(f'The {str(table)} is not a proper type!')

        if 'x' not in df_temp.keys() or 'y' not in df_temp.keys() or index not in df_temp.keys():
            raise TypeError(f'The {str(table)} missed x and y data!')

        if not os.path.exists(f'{outputfolder}ch_{name}.tif'):
            roi_ds = gdal.Open(self._ROI_tif_list[0])
            roi_map = roi_ds.GetRasterBand(1).ReadAsArray()
            roi_map = roi_map.astype(float)

            x_list = list(df_temp['x'])
            y_list = list(df_temp['y'])
            indi_list = list(df_temp[index])

            if np.max(df_temp['x']) > roi_map.shape[1] or np.max(df_temp['y']) > roi_map.shape[0]:
                raise TypeError(f'The df exceed the roi map!')
            else:
                with tqdm(total=len(df_temp), desc=f'feature table {name} to tiffiles',
                          bar_format='{l_bar}{bar:24}{r_bar}{bar:-24b}') as pbar:
                    for _ in range(len(df_temp)):
                        roi_map[y_list[_], x_list[_]] = indi_list[_]
                        pbar.update()

            roi_map[roi_map == -32768] = np.nan
            roi_map[roi_map == 1] = np.nan
            write_raster(roi_ds, roi_map, outputfolder, f'ch_{name}.tif', raster_datatype=gdal.GDT_Float32)
