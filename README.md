# Fermi LAT Sky Explorer — Simple Edition

This edition produces the same scientific output as the modular v2 project while keeping all
application logic in **one Python file: `main.py`**. It downloads NASA's public Fermi LAT weekly
photon FITS files and creates photon-count and summed-energy maps in both equatorial and Galactic
coordinates.

The project is a compact rebuild of the Fall 2021 `astronomy_436_fa2021` work by Dhruv
Jagdishkumar Patel, David Mercanti, Alexis Richardson, and Pratik Shukla.

> The maps are exploratory event visualizations. They are not exposure-corrected flux maps and
> should not be interpreted as a calibrated source analysis.

## Project structure

```text
fermi_lat_sky_explorer_simple/
  main.py             Complete GUI, CLI, downloader, processing, and plotting code
  requirements.txt    Four runtime dependencies
  setup_windows.bat   One-time isolated Python 3.12 setup
  run_windows.bat     Double-click GUI launcher
  .env.example        Optional environment-variable reference
  .gitignore          Keeps large FITS files and generated results out of GitHub
  README.md
```

There are no Python packages, `src` directory, helper modules, or separate test files.

## Features preserved

- Responsive PySide6 desktop interface with progress, cancellation, logs, and map previews.
- Equivalent command-line workflow.
- HTTPS weekly FITS downloads with retries, partial-file resumption, caching, and validation.
- Chunked FITS processing to control RAM use.
- Energy filtering and optional zenith-angle filtering.
- Per-week and combined products.
- Four PNG maps for every generated product set:
  - equatorial photon counts;
  - Galactic photon counts;
  - equatorial summed photon energy;
  - Galactic summed photon energy.
- Compressed histogram arrays, weekly JSON metadata, and a complete run manifest.

## Windows quick start

1. Extract the ZIP into a normal folder such as `Documents\fermi_lat_sky_explorer_simple`.
2. Double-click `setup_windows.bat` once.
3. Double-click `run_windows.bat` whenever you want to use the GUI.

The setup script creates a private `.fermi-env` with Python 3.12. It does not change the Anaconda
base environment, so it also avoids the earlier Python 3.9 compatibility error. `.env` is not a
Windows command and is not required by this application.

If double-click setup cannot locate Conda, open **Anaconda Prompt**, move to the extracted folder,
and run:

```bat
setup_windows.bat
```

## Manual installation

Use Python 3.10 or newer:

```bash
python -m venv .venv
```

Windows activation:

```bat
.venv\Scripts\activate
```

macOS or Linux activation:

```bash
source .venv/bin/activate
```

Install and start:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python main.py
```

## Command-line use

Check the installation:

```bash
python main.py doctor
```

Run an analysis:

```bash
python main.py run --start-week 9 --end-week 10 --energy-min 100 --energy-max 100000 --bins 360
```

Useful options:

```text
--combined-only             Skip per-week products
--max-zenith-angle 90       Apply a zenith-angle selection
--data-dir PATH             Choose the FITS cache directory
--output-dir PATH           Choose the result directory
--chunk-rows 500000         Control peak processing memory
--force-download            Replace cached weekly FITS files
--no-energy-filter          Select all finite positive photon energies
```

Run `python main.py run --help` to see every option.

## Output structure

Each run creates a timestamped result directory:

```text
outputs/
  run_YYYYMMDDTHHMMSSZ_w009-w010/
    manifest.json
    combined/
      histograms.npz
      counts_equatorial.png
      counts_galactic.png
      energy_sum_equatorial.png
      energy_sum_galactic.png
    week_009/
      histograms.npz
      metadata.json
      counts_equatorial.png
      counts_galactic.png
      energy_sum_equatorial.png
      energy_sum_galactic.png
```

Downloaded weekly FITS files remain in `data/` and are reused. Both `data/` and `outputs/` are
excluded from Git, which keeps the repository small while preserving reproducibility.

## Configuration

Most users should choose settings in the GUI or CLI. Optional deployment defaults can be set with
the environment variables listed in `.env.example`; the project requires no credentials or API
keys. The default weekly filename is:

```text
lat_photon_weekly_w{week:03d}_p305_v001.fits
```

If the NASA archive publishes a different processing version, set `FERMI_SKY_FILE_PATTERN` to the
new pattern without modifying `main.py`.
