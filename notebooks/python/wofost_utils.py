#!/usr/bin/env python
"""Some functions to create WOFOST ensembles on the fly."""
import datetime as dt
from io import BytesIO
from pathlib import Path

import ipywidgets.widgets as widgets
import numpy as np
import pandas as pd
import scipy.stats as ss
from bs4 import BeautifulSoup
from ipywidgets import fixed, interact, interactive
from pcse.base import ParameterProvider
from pcse.fileinput import (
    CABOFileReader,
    CSVWeatherDataProvider,
    YAMLAgroManagementReader,
)
from pcse.models import Wofost71_PP, Wofost71_WLP_FD
from pcse.util import WOFOST71SiteDataProvider


from functools import partial
from multiprocessing import Pool
from textwrap import dedent
from tqdm.contrib.concurrent import process_map


import ee
import requests
from retry import retry

agromanagement_contents = """
Version: 1.0
AgroManagement:
- {year:d}-01-01:
    CropCalendar:
        crop_name: '{crop}'
        variety_name: '{variety}'
        crop_start_date: {crop_start_date}
        crop_start_type: sowing
        crop_end_date: {crop_end_date}
        crop_end_type: harvest
        max_duration: 150
    TimedEvents: null
    StateEvents: null
"""
WOFOST_PARAMETERS = [
    "DVS",
    "LAI",
    "TAGP",
    "TWSO",
    "TWLV",
    "TWST",
    "TWRT",
    "TRA",
    "RD",
    "SM",
]
LABELS = [
    "Development stage [-]",
    "LAI [m2/m2]",
    "Total Biomass [kg/ha]",
    "Total Storage Organ Weight [kg/ha]",
    "Total Leaves Weight [kg/ha]",
    "Total Stems Weight [kg/ha]",
    "Total Root Weight [kg/ha]",
    "Transpiration rate [cm/d]",
    "Rooting depth [cm]",
    "Soil moisture [cm3/cm3]",
]
WOFOST_LABELS = dict(zip(WOFOST_PARAMETERS, LABELS))


# ee.Authenticate()

ee.Initialize(opt_url="https://earthengine-highvolume.googleapis.com")


def get_ensemble_jasmin(
    year,
    lat,
    lon,
    base_url="https://gws-access.jasmin.ac.uk/"
    + "public/odanceo/ghana_ensembles/",
):
    """This function will search on JASMIN for any pre-computed ensembles"""

    html = requests.get(base_url).content
    soup = BeautifulSoup(html, "html.parser")

    # Find all <a> in your HTML that have a not null 'href'. Keep only 'href'.
    links = [
        a["href"]
        for a in soup.find_all("a", href=True)
        if a["href"].endswith(".npz")
    ]

    link_url = None
    for link in links:
        _, fyear, flon, flat, size = link.split("_")
        if year == int(fyear) and lat == float(flat) and lon == float(flon):
            link_url = link
            break
    if link_url is None:
        return None
    print(f"Getting remote version of ensemble!")
    r = requests.get(f"{base_url}/{link_url}", stream=True)
    data = np.load(BytesIO(r.raw.read()), allow_pickle=True)
    return data


def write_pcse_csv(
    variables,
    elev,
    lon,
    lat,
    csv_file,
    c1=-0.18,
    c2=-0.55,
):
    """Write a PCSE-friendly CSV meteo file. Uses the data I have stored in
    JASMIN (ERA5)

    Parameters
    ----------
    data: array
        The actual daily data as a pandas data frame. Should have columns
        `DAY`, `IRRAD`, `TMIN`, `TMAX`, `VAP`, `WIND`, `RAIN`, in standard
        WOFOST units.
    elev: float
        Elevation in m ASL
    lon : float
        Longitude in decimal degrees.
    lat : float
        Latitude in decimal degrees
    csv_file : str
        CSV filename to store
    c1 : float, optional
        The `c1` parameter, by default -0.18
    c2 : float, optional
        The `c2` parameter, by default -0.55

    Returns
    A pathlib object to the CSV file.
    """

    # if file exists, return old file

    # elev = retrieve_pixel_value(lon, lat, DEM_FILE)
    country = "somewhere"
    site_name = "anything"
    hdr_chunk = f"""Country     = '{country}'
Station     = '{site_name}'
Description = 'Reanalysis data'
Source      = 'ERA5'
Contact     = 'J Gomez-Dans'
Longitude = {lon}; Latitude = {lat}; Elevation = {elev}; AngstromA = {c1}; AngstromB = {c2}; HasSunshine = False
## Daily weather observations (missing values are NaN)
    """
    hdr_chunk = dedent(hdr_chunk)
    variables["SNOWDEPATH"] = variables["IRRAD"] * np.nan
    variables.columns = [
        "DAY",
        "IRRAD",
        "TMIN",
        "TMAX",
        "VAP",
        "WIND",
        "RAIN",
        "SNOWDEPTH",
    ]
    with csv_file.open(mode="w", newline="") as fp:
        fp.write(hdr_chunk)
        variables.to_csv(fp, index=False, na_rep="nan")
    return csv_file


def get_weather_images(parameter, year):
    """Gets meteo data from EarthEngine"""
    dataset = (
        ee.ImageCollection("ECMWF/ERA5_LAND/HOURLY")
        .filter(ee.Filter.date("%d-01-01" % year, "%d-01-01" % (year + 1)))
        .select([parameter])
        .toBands()
    )
    return dataset


@retry(tries=10, delay=1, backoff=2)
def download_image_over_region(image, geom, scale):
    return image.reduceRegion(ee.Reducer.mean(), geom, scale).getInfo()


def calculate_hum(tdew):
    tdew = tdew - 273.15
    tmp = (17.27 * tdew) / (tdew + 237.3)
    ea = 0.6108 * np.exp(tmp)
    return ea


def get_era5_gee(year, lat, lon, dest_folder="data/"):
    """Get Meteo from EarthEngine and produce a WOFOST-friendly
    meteo file"""
    geom = ee.Geometry.Point(lon, lat).buffer(5500)

    country = "Somewhere"
    site = "anything"
    product = "ERA5"
    lat_str = "%.02f" % lat
    lon_str = "%.02f" % lon

    csv_file = (
        Path(dest_folder)
        / f"{product}_{country}_{lat_str}_{lon_str}_{year}.csv"
    )
    if csv_file.exists():
        return csv_file

    parameters = [
        "dewpoint_temperature_2m",
        "temperature_2m",
        "surface_solar_radiation_downwards_hourly",
        "total_precipitation_hourly",
        "u_component_of_wind_10m",
        "v_component_of_wind_10m",
    ]

    images = [get_weather_images(parameter, year) for parameter in parameters]
    images += [ee.Image("USGS/GTOPO30").select("elevation")]

    par = partial(download_image_over_region, geom=geom, scale=11132)
    pool = Pool(len(images))
    ret = pool.map(par, images)
    pool.close()
    pool.join()

    ret = {k: v for d in ret for k, v in d.items()}

    transforms = [
        lambda val: calculate_hum(val),
        lambda val: val - 273.15,
        lambda val: val / 1000,
        lambda val: val * 1000,
        lambda val: val,
        lambda val: val,
    ]
    transforms = dict(zip(parameters, transforms))

    total_days = (dt.datetime(year + 1, 1, 1) - dt.datetime(year, 1, 1)).days
    available_keys = ret.keys()

    para_vals = []
    for parameter in parameters:
        tranform = transforms[parameter]
        vals = []
        for i in range(total_days):
            date = dt.datetime(year, 1, 1) + dt.timedelta(days=i)
            date_str = date.strftime("%Y%m%d")
            for hour in range(24):
                header = "%sT%02d_%s" % (date_str, hour, parameter)
                if header in available_keys:
                    if ret[header] is not None:
                        val = tranform(ret[header])
                    else:
                        val = np.nan
                else:
                    val = np.nan
                vals.append(val)
        para_vals.append(vals)

    para_vals = np.array(para_vals).reshape(6, total_days, 24)

    humidity = np.maximum(np.nanmean(para_vals[0], axis=1), 0)
    temperature_2m_max = np.nanmax(para_vals[1], axis=1)
    temperature_2m_min = np.nanmin(para_vals[1], axis=1)
    solar_radiation = np.maximum(np.nansum(para_vals[2], axis=1), 0)
    total_precipitation = np.maximum(np.nansum(para_vals[3], axis=1), 0)
    wind = np.nanmean(np.sqrt(para_vals[4] ** 2 + para_vals[5] ** 2), axis=1)
    elev = ret["elevation"]

    keys = ["DAY", "IRRAD", "TMIN", "TMAX", "VAP", "WIND", "RAIN"]
    vals = [
        pd.date_range("%d-01-01" % year, "%d-12-31" % year),
        solar_radiation,
        temperature_2m_min,
        temperature_2m_max,
        humidity,
        wind,
        total_precipitation,
    ]
    df = pd.DataFrame(dict(zip(keys, vals)))
    csv_file = write_pcse_csv(df, elev, lon, lat, csv_file)
    return csv_file


def define_prior_distribution(
    fname="data/par_prior_maize_tropical-C.csv", tsum1=None, tsum2=None
):
    df = pd.read_csv(fname)
    cols = df.columns.str.replace("#", "")
    df.columns = cols
    df.Distribution = df.Distribution.astype(str)
    prior_dist = {
        k.PARAM_CODE: ss.uniform(k.Min, (k.Max - k.Min))
        for i, k in df[df.Distribution == "nan"].iterrows()
    }

    tmp = {
        k.PARAM_CODE: ss.truncnorm(
            (k.Min - k.PARAM_YVALUE) / k.StdDev,
            (k.Max - k.PARAM_YVALUE) / k.StdDev,
            loc=k.PARAM_YVALUE,
            scale=k.StdDev,
        )
        for i, k in df[df.Distribution == "Gaussian"].iterrows()
    }
    prior_dist.update(tmp)
    param_list = df.PARAM_CODE.values.tolist()
    param_xvalue = df.PARAM_XVALUE.values.tolist()
    param_yvalue = df.PARAM_YVALUE.values.tolist()
    param_type = dict(zip(param_list, df.Variation.values.tolist()))
    param_scale = dict(zip(param_list, df.Scale.values.tolist()))
    return (
        prior_dist,
        param_list,
        param_xvalue,
        param_yvalue,
        param_type,
        param_scale,
    )


def run_wofost(parameters, agromanagement, wdp, potential=False):
    if potential:
        wofsim = Wofost71_PP(parameters, wdp, agromanagement)
    else:
        wofsim = Wofost71_WLP_FD(parameters, wdp, agromanagement)

    wofsim.run_till_terminate()
    df_results = pd.DataFrame(wofsim.get_output())
    df_results = df_results.set_index("day")
    return df_results, wofsim


def wofost_parameter_sweep_func(
    year,
    ens_parameters,
    meteo="data/AgERA5_Togo_Tamale_2021_2022.csv",
    wav=20,
    cropfile="data/MAIZGA-C.CAB",
    soil="data/ec4.new",
    co2=400,
    rdmsol=100.0,
    potential=False,
):
    cropdata = CABOFileReader("data/MAIZGA-C.CAB")

    soildata = CABOFileReader(soil)
    soildata["RDMSOL"] = rdmsol
    sitedata = WOFOST71SiteDataProvider(WAV=wav, CO2=co2)
    parameters = ParameterProvider(
        cropdata=cropdata, soildata=soildata, sitedata=sitedata
    )
    sowing_doy = int(np.round(ens_parameters.pop("SDOY")))
    for k, v in ens_parameters.items():
        parameters.set_override(k, v, check=True)
    crop_start_date = dt.datetime.strptime(
        f"{year}/{sowing_doy}", "%Y/%j"
    ).date()
    crop_end_date = dt.date(year, 11, 30)
    with open("data/temporal.amgt", "w") as fp:
        fp.write(
            agromanagement_contents.format(
                year=crop_start_date.year,
                crop="maize",
                variety="Ghana",
                crop_start_date=crop_start_date,
                crop_end_date=crop_end_date,
            )
        )
    agromanagement = YAMLAgroManagementReader("data/temporal.amgt")

    wdp = CSVWeatherDataProvider(meteo, dateformat="%Y-%m-%d", delimiter=",")

    df_results, simulator = run_wofost(
        parameters, agromanagement, wdp, potential=potential
    )
    return df_results
    # df_results.to_csv(key, encoding="utf-8", index=False)


def create_ensemble(
    lat,
    lon,
    year,
    en_size=10,
    param_file="data/par_prior_maize_tropical-C.csv",
    cropfile="data/MAIZGA-C.CAB",
    soil="data/ec4.new",
    co2=400,
    rdmsol=100.0,
    potential=False,
):

    ensemble_fname = (
        "ensMaizeWLLlowest_"
        + f"{year:4d}_{lon:.2f}_{lat:.2f}"
        + f"_size{en_size:d}.npz"
    )
    if Path(ensemble_fname).exists():
        # No need to do anything, just read it in:
        return np.load(ensemble_fname, allow_pickle=True)
    else:
        # Check whether it's been pregenerated online
        retval = get_ensemble_jasmin(year, lat, lon)
        if retval is not None:
            return retval

    print("Getting meteo data from EarthEngine")
    meteo_file = get_era5_gee(year, lat, lon, dest_folder="./")
    (
        prior_dist,
        param_list,
        param_xvalue,
        param_yvalue,
        param_type,
        param_scale,
    ) = define_prior_distribution(fname=param_file)
    z_start = np.empty((len(param_list), en_size))
    for i, param in enumerate(param_list):
        if prior_dist[param] == 0:
            z_start[i, :] = (
                np.ones(en_size) * param_xvalue[param] * param_scale[param]
            )
        else:
            z_start[i, :] = prior_dist[param].rvs(en_size) * param_scale[param]
    ensemble_parameters = []
    for i in range(en_size):
        dd = {}
        for j, parameter_name in enumerate(param_list):
            if param_type[parameter_name] == "S":
                dd[parameter_name] = z_start[j, i]
        amaxtb = [
            0,
            z_start[param_list.index("AMAXTB_000"), i],
            1.25,
            z_start[param_list.index("AMAXTB_000"), i],
            1.5,
            z_start[param_list.index("AMAXTB_150"), i],
        ]
        dd["AMAXTB"] = amaxtb
        ensemble_parameters.append(dd)

    results = []
    wrapper = partial(
        wofost_parameter_sweep_func,
        year,
        meteo=meteo_file,
        cropfile=cropfile,
        soil=soil,
        co2=co2,
        rdmsol=rdmsol,
        potential=potential,
    )

    results = process_map(wrapper, ensemble_parameters)
    ###for sample in ensemble_parameters:
    ####results.append(
    ####    wofost_parameter_sweep_func(
    ####        sample,
    ####        year,
    ####        meteo=meteo_file,
    ####        cropfile=cropfile,
    ####        soil=soil,
    ####        co2=co2,
    ####        rdmsol=rdmsol,
    ####        potential=potential,
    ####    )
    ###results.append(wrapper(sample))

    return results


if __name__ == "__main__":
    lon = -2.7
    lat = 8.20
    year = 2020
    retval = create_ensemble(
        lat, lon, year, en_size=5, param_file="data/par_prior_maize_tropical-C.csv"
    )

    lon = -0.7
    lat = 9.5
    year = 2021
    retval = create_ensemble(
        lat,
        lon,
        year,
        en_size=500,
        param_file="data/par_prior_maize_tropical-C.csv",
    )
########def wofost_parameter_sweep():
########widgets.interact_manual(wofost_parameter_sweep_func,
########field_code=widgets.Dropdown(options=all_fields),
########crop_start_date=widgets.fixed(dt.date(2021, 7, 25)),
########crop_end_date=widgets.fixed(dt.date(2021, 12, 1)),
########span=widgets.FloatSlider(value=35.0, min=10, max=50),
########cvo = widgets.FloatSlider(value=0.54, min=0.1, max=0.9, step=0.02),
########cvl = widgets.FloatSlider(value=0.68, min=0.1, max=0.9, step=0.02),
########tdwi=widgets.FloatSlider(value=2.0,min=0.5, max=20),
########amaxtb_000=widgets.FloatSlider(value=55.0,min=0, max=90),
########amaxtb_150=widgets.FloatSlider(value=55.0,min=0, max=90),
########tsum1=widgets.FloatSlider(value=850.0,min=100, max=1800),
########tsum2=widgets.FloatSlider(value=850.0,min=100, max=1800),
########tsumem=widgets.FloatSlider(value=56,min=10, max=100),
########rgrlai=widgets.FloatSlider(value=0.005, min=0.001, max=0.3, step=0.001,
########readout_format='.3f',),
########meteo=widgets.fixed("AgERA5_Togo_Tamale_2021_2022.csv"),
########soil=widgets.fixed("ec4.new"),
########wav=widgets.FloatSlider(value=5, min=0, max=100),
########co2=widgets.fixed(400),
########rdmsol=widgets.fixed(100.),
########potential=widgets.Checkbox(value=True, description='Potential mode',
########icon='check'))
