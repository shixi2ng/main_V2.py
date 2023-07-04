from RSDatacube.RSdc import *


if __name__ == '__main__':
    # Landsat_WI_temp = Landsat_dc('G:\\A_Landsat_veg\\Landsat_floodplain_2020_datacube\\MNDWI_datacube')
    # Landsat_dcs = RS_dcs(Landsat_WI_temp)
    # Landsat_dcs.inundation_detection('DT', 'MNDWI', 'Landsat')

    # Landsat_VI_temp = Landsat_dc('G:\\A_Landsat_veg\\Landsat_floodplain_2020_datacube\\OSAVI_datacube')
    # Landsat_inun_temp = Landsat_dc('G:\\A_Landsat_veg\\Landsat_floodplain_2020_datacube\\Inundation_DT_datacube')
    # Landsat_dcs = RS_dcs(Landsat_inun_temp, Landsat_VI_temp)
    # Landsat_dcs.inundation_removal('OSAVI', 'DT', 'Landsat', append_new_dc=False)

    # Landsat_VI_temp = Landsat_dc('G:\\A_Landsat_veg\\Landsat_floodplain_2020_datacube\\OSAVI_noninun_datacube\\')
    # Landsat_dcs = RS_dcs(Landsat_VI_temp)
    # Landsat_dcs.curve_fitting('OSAVI_noninun', 'Landsat')

    for year in [str(_) for _ in range(1996, 2023)]:
        dc_temp_dic = Phemetric_dc(f'G:\\A_Landsat_veg\\Landsat_floodplain_2020_datacube\\OSAVI_noninun_curfit_datacube\\floodplain_2020_Phemetric_datacube\\{year}\\')
        dc_temp_dic.calculate_phemetrics(['MAVI'])