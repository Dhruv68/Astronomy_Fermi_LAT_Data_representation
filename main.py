"""Fermi LAT Sky Explorer in one Python file.

Downloads public weekly Fermi LAT photon FITS files, filters events in bounded
chunks, and creates count and summed-energy sky maps in equatorial and Galactic
coordinates. The same pipeline is available through a PySide6 GUI and a CLI.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import logging
import os
import sys
import time
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

import numpy as np
from astropy.io import fits
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.colors import LogNorm
from matplotlib.figure import Figure

QT_IMPORT_ERROR: ImportError | None = None
try:
    from PySide6.QtCore import QObject, QSize, Qt, QThread, QTimer, QUrl, Signal, Slot
    from PySide6.QtGui import QCloseEvent, QDesktopServices, QPixmap, QResizeEvent
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QDoubleSpinBox,
        QFileDialog,
        QFormLayout,
        QFrame,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QListWidget,
        QMainWindow,
        QMessageBox,
        QPlainTextEdit,
        QProgressBar,
        QPushButton,
        QScrollArea,
        QSizePolicy,
        QSpinBox,
        QSplitter,
        QTabWidget,
        QVBoxLayout,
        QWidget,
    )
except ImportError as exc:
    # Keep the scientific CLI usable on minimal/headless systems. run_gui() gives
    # a clear error before any of these definition-only fallbacks can be used.
    QT_IMPORT_ERROR = exc

    class _QtDefinitionFallback:
        pass

    class _QtEnumFallback:
        def __getattr__(self, _name: str) -> _QtEnumFallback:
            return self

        def __or__(self, _other: object) -> _QtEnumFallback:
            return self

    class _QtSignalFallback:
        def connect(self, *_args: object, **_kwargs: object) -> None:
            return None

        def emit(self, *_args: object, **_kwargs: object) -> None:
            return None

    def Signal(*_args: object, **_kwargs: object) -> _QtSignalFallback:  # type: ignore[misc]
        return _QtSignalFallback()

    def Slot(*_args: object, **_kwargs: object) -> Callable[[Any], Any]:  # type: ignore[misc]
        return lambda function: function

    Qt = _QtEnumFallback()  # type: ignore[assignment]
    QObject = QMainWindow = _QtDefinitionFallback  # type: ignore[misc,assignment]
    QSize = QThread = QTimer = QUrl = _QtDefinitionFallback  # type: ignore[misc,assignment]
    QCloseEvent = QDesktopServices = QPixmap = QResizeEvent = _QtDefinitionFallback  # type: ignore[misc,assignment]
    QApplication = QCheckBox = QDoubleSpinBox = QFileDialog = _QtDefinitionFallback  # type: ignore[misc,assignment]
    QFormLayout = QFrame = QHBoxLayout = QLabel = QLineEdit = _QtDefinitionFallback  # type: ignore[misc,assignment]
    QListWidget = QMessageBox = QPlainTextEdit = QProgressBar = _QtDefinitionFallback  # type: ignore[misc,assignment]
    QPushButton = QScrollArea = QSizePolicy = QSpinBox = _QtDefinitionFallback  # type: ignore[misc,assignment]
    QSplitter = QTabWidget = QVBoxLayout = QWidget = _QtDefinitionFallback  # type: ignore[misc,assignment]

__version__ = "2.1.0"

DEFAULT_BASE_URL = os.getenv(
    "FERMI_SKY_BASE_URL",
    "https://heasarc.gsfc.nasa.gov/FTP/fermi/data/lat/weekly/photon/",
)
DEFAULT_FILE_PATTERN = os.getenv(
    "FERMI_SKY_FILE_PATTERN",
    "lat_photon_weekly_w{week:03d}_p305_v001.fits",
)
DEFAULT_DATA_DIR = Path(os.getenv("FERMI_SKY_DATA_DIR", "data"))
DEFAULT_OUTPUT_DIR = Path(os.getenv("FERMI_SKY_OUTPUT_DIR", "outputs"))
DEFAULT_LOG_LEVEL = os.getenv("FERMI_SKY_LOG_LEVEL", "INFO").upper()
LOGGER = logging.getLogger("fermi_sky")


# ---------------------------------------------------------------------------
# Errors and validated run models
# ---------------------------------------------------------------------------


class FermiSkyError(Exception):
    """Base class for expected application failures."""


class ConfigurationError(FermiSkyError):
    """Raised when a run setting is invalid."""


class DownloadError(FermiSkyError):
    """Raised when a weekly FITS file cannot be downloaded or validated."""


class FitsFormatError(FermiSkyError):
    """Raised when a FITS file lacks the required event data."""


class ProcessingCancelled(FermiSkyError):
    """Raised after a cooperative cancellation request."""


@dataclass(frozen=True, slots=True)
class EnergyRange:
    """Inclusive photon-energy selection in MeV."""

    minimum_mev: float | None = 100.0
    maximum_mev: float | None = 100_000.0

    def __post_init__(self) -> None:
        values = (self.minimum_mev, self.maximum_mev)
        if any(value is not None and (not np.isfinite(value) or value < 0) for value in values):
            raise ConfigurationError("Energy bounds must be finite, non-negative MeV values.")
        if (
            self.minimum_mev is not None
            and self.maximum_mev is not None
            and self.minimum_mev >= self.maximum_mev
        ):
            raise ConfigurationError("Maximum energy must be greater than minimum energy.")

    @property
    def label(self) -> str:
        if self.minimum_mev is None and self.maximum_mev is None:
            return "all available energies"
        lower = "unbounded" if self.minimum_mev is None else f"{self.minimum_mev:g}"
        upper = "unbounded" if self.maximum_mev is None else f"{self.maximum_mev:g}"
        return f"{lower}–{upper} MeV"

    def as_dict(self) -> dict[str, float | None]:
        return {"minimum_mev": self.minimum_mev, "maximum_mev": self.maximum_mev}


@dataclass(frozen=True, slots=True)
class RunConfig:
    """Complete validated configuration for one visualization run."""

    start_week: int
    end_week: int
    energy: EnergyRange = field(default_factory=EnergyRange)
    bins: int = 360
    chunk_rows: int = 500_000
    max_zenith_angle_deg: float | None = None
    create_per_week: bool = True
    force_download: bool = False
    data_dir: Path = DEFAULT_DATA_DIR
    output_dir: Path = DEFAULT_OUTPUT_DIR
    base_url: str = DEFAULT_BASE_URL
    file_pattern: str = DEFAULT_FILE_PATTERN

    def __post_init__(self) -> None:
        if self.start_week < 0 or self.end_week < 0:
            raise ConfigurationError("Week numbers cannot be negative.")
        if self.end_week < self.start_week:
            raise ConfigurationError("End week must be greater than or equal to start week.")
        if self.end_week - self.start_week + 1 > 520:
            raise ConfigurationError("A single run is limited to 520 weeks.")
        if not 36 <= self.bins <= 1440:
            raise ConfigurationError("Bins must be between 36 and 1440.")
        if self.bins % 2:
            raise ConfigurationError("Bins must be an even number.")
        if not 10_000 <= self.chunk_rows <= 5_000_000:
            raise ConfigurationError("Chunk rows must be between 10,000 and 5,000,000.")
        if self.max_zenith_angle_deg is not None:
            if not np.isfinite(self.max_zenith_angle_deg):
                raise ConfigurationError("Maximum zenith angle must be finite.")
            if not 0 < self.max_zenith_angle_deg <= 180:
                raise ConfigurationError("Maximum zenith angle must be in (0, 180] degrees.")
        if not self.base_url.lower().startswith("https://"):
            raise ConfigurationError("The Fermi archive base URL must use HTTPS.")
        if "{week" not in self.file_pattern:
            raise ConfigurationError("The file pattern must contain a {week...} field.")
        object.__setattr__(self, "data_dir", Path(self.data_dir).expanduser().resolve())
        object.__setattr__(self, "output_dir", Path(self.output_dir).expanduser().resolve())

    @property
    def weeks(self) -> range:
        return range(self.start_week, self.end_week + 1)

    def filename_for_week(self, week: int) -> str:
        try:
            filename = self.file_pattern.format(week=week)
        except (KeyError, ValueError, IndexError) as exc:
            raise ConfigurationError(f"Invalid weekly file pattern: {exc}") from exc
        if Path(filename).name != filename:
            raise ConfigurationError("The weekly file pattern must produce a filename, not a path.")
        return filename

    def as_dict(self) -> dict[str, Any]:
        return {
            "start_week": self.start_week,
            "end_week": self.end_week,
            "energy": self.energy.as_dict(),
            "bins": self.bins,
            "chunk_rows": self.chunk_rows,
            "max_zenith_angle_deg": self.max_zenith_angle_deg,
            "create_per_week": self.create_per_week,
            "force_download": self.force_download,
            "data_dir": str(self.data_dir),
            "output_dir": str(self.output_dir),
            "base_url": self.base_url,
            "file_pattern": self.file_pattern,
        }


@dataclass(slots=True)
class SkyHistograms:
    """Incrementally accumulated full-sky histogram products."""

    longitude_edges_deg: np.ndarray
    latitude_edges_deg: np.ndarray
    counts_equatorial: np.ndarray
    counts_galactic: np.ndarray
    energy_equatorial_mev: np.ndarray
    energy_galactic_mev: np.ndarray
    input_events: int = 0
    selected_events: int = 0
    selected_energy_mev: float = 0.0

    @classmethod
    def empty(cls, bins: int) -> SkyHistograms:
        shape = (bins, bins // 2)
        longitude_edges = np.linspace(-180.0, 180.0, bins + 1, dtype=np.float64)
        latitude_edges = np.linspace(-90.0, 90.0, bins // 2 + 1, dtype=np.float64)
        return cls(
            longitude_edges_deg=longitude_edges,
            latitude_edges_deg=latitude_edges,
            counts_equatorial=np.zeros(shape, dtype=np.float64),
            counts_galactic=np.zeros(shape, dtype=np.float64),
            energy_equatorial_mev=np.zeros(shape, dtype=np.float64),
            energy_galactic_mev=np.zeros(shape, dtype=np.float64),
        )

    def add(self, other: SkyHistograms) -> None:
        if self.counts_equatorial.shape != other.counts_equatorial.shape:
            raise ValueError("Cannot combine histograms with different bin shapes.")
        self.counts_equatorial += other.counts_equatorial
        self.counts_galactic += other.counts_galactic
        self.energy_equatorial_mev += other.energy_equatorial_mev
        self.energy_galactic_mev += other.energy_galactic_mev
        self.input_events += other.input_events
        self.selected_events += other.selected_events
        self.selected_energy_mev += other.selected_energy_mev


@dataclass(frozen=True, slots=True)
class PipelineResult:
    run_dir: Path
    manifest_path: Path
    image_paths: tuple[Path, ...]
    selected_events: int
    selected_energy_mev: float


def configure_logging(level: str = DEFAULT_LOG_LEVEL) -> None:
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
    )


# ---------------------------------------------------------------------------
# Reliable weekly FITS download
# ---------------------------------------------------------------------------


DownloadProgress = Callable[[int, int | None, str], None]
CancelCheck = Callable[[], bool]


def looks_like_fits(path: Path) -> bool:
    """Perform a fast FITS header signature and block-size check."""

    try:
        if not path.is_file() or path.stat().st_size < 2880:
            return False
        with path.open("rb") as stream:
            signature = stream.read(30)
        return signature.startswith(b"SIMPLE  =")
    except OSError:
        return False


class WeeklyFileDownloader:
    """Download weekly files with retries, resumption, and atomic replacement."""

    def __init__(self, *, timeout_seconds: float = 60.0, retries: int = 3) -> None:
        self.timeout_seconds = timeout_seconds
        self.retries = retries

    def fetch(
        self,
        *,
        base_url: str,
        filename: str,
        destination_dir: Path,
        force: bool = False,
        progress: DownloadProgress | None = None,
        is_cancelled: CancelCheck | None = None,
    ) -> Path:
        destination_dir.mkdir(parents=True, exist_ok=True)
        target = destination_dir / filename
        partial = target.with_suffix(target.suffix + ".part")

        if not force and looks_like_fits(target):
            LOGGER.info("Using cached FITS file: %s", target)
            if progress:
                size = target.stat().st_size
                progress(size, size, f"Using cached {filename}")
            return target
        if force:
            partial.unlink(missing_ok=True)

        url = urljoin(base_url.rstrip("/") + "/", filename)
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            if is_cancelled and is_cancelled():
                raise ProcessingCancelled("Download cancelled.")
            try:
                self._download_once(
                    url=url,
                    filename=filename,
                    partial=partial,
                    progress=progress,
                    is_cancelled=is_cancelled,
                )
                if not looks_like_fits(partial):
                    raise DownloadError(
                        f"Downloaded file {filename} does not have a valid FITS header."
                    )
                os.replace(partial, target)
                LOGGER.info("Downloaded %s", target)
                return target
            except ProcessingCancelled:
                raise
            except HTTPError as exc:
                if exc.code == 404:
                    raise DownloadError(
                        f"Week file not found at NASA HEASARC: {filename}. "
                        "Check the week number or file-pattern setting."
                    ) from exc
                last_error = exc
            except (DownloadError, OSError, URLError, TimeoutError) as exc:
                last_error = exc
            if attempt < self.retries:
                wait_seconds = 2 ** (attempt - 1)
                LOGGER.warning(
                    "Download attempt %s/%s failed for %s: %s; retrying in %ss",
                    attempt,
                    self.retries,
                    filename,
                    last_error,
                    wait_seconds,
                )
                time.sleep(wait_seconds)

        raise DownloadError(
            f"Could not download {filename} after {self.retries} attempts: {last_error}"
        ) from last_error

    def _download_once(
        self,
        *,
        url: str,
        filename: str,
        partial: Path,
        progress: DownloadProgress | None,
        is_cancelled: CancelCheck | None,
    ) -> None:
        existing_size = partial.stat().st_size if partial.exists() else 0
        headers = {"User-Agent": f"FermiLATSkyExplorer/{__version__}"}
        if existing_size:
            headers["Range"] = f"bytes={existing_size}-"

        request = Request(url, headers=headers)
        with urlopen(request, timeout=self.timeout_seconds) as response:
            response_status = getattr(response, "status", 200)
            resumed = existing_size > 0 and response_status == 206
            mode = "ab" if resumed else "wb"
            completed = existing_size if resumed else 0
            content_length = response.headers.get("Content-Length")
            total = completed + int(content_length) if content_length else None

            with partial.open(mode) as stream:
                while True:
                    if is_cancelled and is_cancelled():
                        raise ProcessingCancelled("Download cancelled.")
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    stream.write(chunk)
                    completed += len(chunk)
                    if progress:
                        progress(completed, total, f"Downloading {filename}")
                stream.flush()
                os.fsync(stream.fileno())

            if total is not None and completed != total:
                raise DownloadError(
                    f"Incomplete download for {filename}: received {completed:,} "
                    f"of {total:,} bytes."
                )


# ---------------------------------------------------------------------------
# Memory-bounded FITS processing
# ---------------------------------------------------------------------------


ProcessProgress = Callable[[int, int, str], None]
REQUIRED_COLUMNS = {"RA", "DEC", "L", "B", "ENERGY"}


def astronomy_longitude(longitude_deg: np.ndarray) -> np.ndarray:
    """Wrap longitude to [-180, 180) and make it increase to the left."""

    wrapped = (np.asarray(longitude_deg, dtype=np.float64) + 180.0) % 360.0 - 180.0
    return -wrapped


def _event_hdu(hdul: fits.HDUList) -> fits.BinTableHDU:
    if "EVENTS" in hdul:
        candidate = hdul["EVENTS"]
        if isinstance(candidate, fits.BinTableHDU):
            return candidate
    for candidate in hdul:
        if not isinstance(candidate, fits.BinTableHDU) or candidate.data is None:
            continue
        names = {name.upper() for name in (candidate.columns.names or [])}
        if REQUIRED_COLUMNS.issubset(names):
            return candidate
    raise FitsFormatError("No binary EVENTS table containing RA, DEC, L, B, and ENERGY was found.")


def _header_value(value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, np.generic):
        return value.item()
    return str(value)


def process_weekly_fits(
    path: Path,
    config: RunConfig,
    *,
    progress: ProcessProgress | None = None,
    is_cancelled: CancelCheck | None = None,
) -> tuple[SkyHistograms, dict[str, Any]]:
    """Process one weekly FITS file without loading the full event table into RAM."""

    LOGGER.info("Processing FITS file: %s", path)
    histograms = SkyHistograms.empty(config.bins)
    try:
        with fits.open(path, mode="readonly", memmap=True, lazy_load_hdus=True) as hdul:
            events_hdu = _event_hdu(hdul)
            data = events_hdu.data
            if data is None:
                raise FitsFormatError(f"The EVENTS table in {path.name} is empty.")

            column_names = {name.upper() for name in (events_hdu.columns.names or [])}
            missing = REQUIRED_COLUMNS - column_names
            if missing:
                raise FitsFormatError(
                    f"{path.name} is missing required columns: {', '.join(sorted(missing))}."
                )
            if config.max_zenith_angle_deg is not None and "ZENITH_ANGLE" not in column_names:
                raise FitsFormatError(
                    f"A zenith-angle limit was requested, but {path.name} has no "
                    "ZENITH_ANGLE column."
                )

            total_rows = len(data)
            histograms.input_events = total_rows
            header = events_hdu.header
            metadata = {
                "input_file": path.name,
                "input_file_bytes": path.stat().st_size,
                "events_hdu": events_hdu.name,
                "input_events": total_rows,
                "telescope": _header_value(header.get("TELESCOP")),
                "instrument": _header_value(header.get("INSTRUME")),
                "tstart": _header_value(header.get("TSTART")),
                "tstop": _header_value(header.get("TSTOP")),
                "date_created": _header_value(header.get("DATE")),
                "pass_version": _header_value(header.get("PASS_VER")),
            }

            for start in range(0, total_rows, config.chunk_rows):
                if is_cancelled and is_cancelled():
                    raise ProcessingCancelled("FITS processing cancelled.")
                stop = min(start + config.chunk_rows, total_rows)
                chunk = data[start:stop]

                ra = np.asarray(chunk["RA"], dtype=np.float64)
                dec = np.asarray(chunk["DEC"], dtype=np.float64)
                gal_l = np.asarray(chunk["L"], dtype=np.float64)
                gal_b = np.asarray(chunk["B"], dtype=np.float64)
                energy = np.asarray(chunk["ENERGY"], dtype=np.float64)

                selected = (
                    np.isfinite(ra)
                    & np.isfinite(dec)
                    & np.isfinite(gal_l)
                    & np.isfinite(gal_b)
                    & np.isfinite(energy)
                    & (energy > 0)
                    & (dec >= -90.0)
                    & (dec <= 90.0)
                    & (gal_b >= -90.0)
                    & (gal_b <= 90.0)
                )
                if config.energy.minimum_mev is not None:
                    selected &= energy >= config.energy.minimum_mev
                if config.energy.maximum_mev is not None:
                    selected &= energy <= config.energy.maximum_mev
                if "CONVERSION_TYPE" in column_names:
                    conversion_type = np.asarray(chunk["CONVERSION_TYPE"])
                    selected &= np.isin(conversion_type, (0, 1))
                if config.max_zenith_angle_deg is not None:
                    zenith = np.asarray(chunk["ZENITH_ANGLE"], dtype=np.float64)
                    selected &= np.isfinite(zenith) & (zenith <= config.max_zenith_angle_deg)

                if np.any(selected):
                    selected_energy = energy[selected]
                    equatorial_longitude = astronomy_longitude(ra[selected])
                    galactic_longitude = astronomy_longitude(gal_l[selected])
                    selected_dec = dec[selected]
                    selected_gal_b = gal_b[selected]
                    bins = (histograms.longitude_edges_deg, histograms.latitude_edges_deg)

                    histograms.counts_equatorial += np.histogram2d(
                        equatorial_longitude, selected_dec, bins=bins
                    )[0]
                    histograms.counts_galactic += np.histogram2d(
                        galactic_longitude, selected_gal_b, bins=bins
                    )[0]
                    histograms.energy_equatorial_mev += np.histogram2d(
                        equatorial_longitude,
                        selected_dec,
                        bins=bins,
                        weights=selected_energy,
                    )[0]
                    histograms.energy_galactic_mev += np.histogram2d(
                        galactic_longitude,
                        selected_gal_b,
                        bins=bins,
                        weights=selected_energy,
                    )[0]
                    histograms.selected_events += int(np.count_nonzero(selected))
                    histograms.selected_energy_mev += float(
                        np.sum(selected_energy, dtype=np.float64)
                    )

                if progress:
                    progress(stop, total_rows, f"Processing {path.name}")

            metadata["selected_events"] = histograms.selected_events
            metadata["selected_energy_mev"] = histograms.selected_energy_mev
            return histograms, metadata
    except (ProcessingCancelled, FitsFormatError):
        raise
    except Exception as exc:
        raise FitsFormatError(f"Could not process {path.name}: {exc}") from exc


# ---------------------------------------------------------------------------
# PNG plotting and run orchestration
# ---------------------------------------------------------------------------


def _positive_log_norm(values: np.ndarray) -> LogNorm | None:
    positive = values[np.isfinite(values) & (values > 0)]
    if positive.size == 0:
        return None
    minimum = float(np.min(positive))
    maximum = float(np.max(positive))
    if maximum <= minimum:
        maximum = minimum * 1.01
    return LogNorm(vmin=minimum, vmax=maximum)


def _longitude_tick_labels(coordinate_system: str) -> list[str]:
    if coordinate_system == "equatorial":
        return ["150°", "120°", "60°", "0°", "300°", "240°", "210°"]
    return ["150°", "120°", "60°", "0°", "−60°", "−120°", "−150°"]


def render_sky_map(
    values: np.ndarray,
    longitude_edges_deg: np.ndarray,
    latitude_edges_deg: np.ndarray,
    *,
    output_path: Path,
    title: str,
    coordinate_system: str,
    colorbar_label: str,
    energy: EnergyRange,
    selected_events: int,
    cmap: str,
) -> Path:
    """Render one histogram as a log-scaled all-sky Mollweide PNG."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    figure = Figure(figsize=(12, 7), dpi=140)
    figure.subplots_adjust(left=0.07, right=0.97, top=0.82, bottom=0.16)
    FigureCanvasAgg(figure)
    axis = figure.add_subplot(111, projection="mollweide")
    axis.set_facecolor("#0b1020")
    axis.grid(True, color="#ffffff", alpha=0.18, linewidth=0.6)

    norm = _positive_log_norm(values)
    if norm is None:
        axis.text(
            0.5,
            0.52,
            "No photons matched the selection",
            transform=axis.transAxes,
            ha="center",
            va="center",
            color="#eef2ff",
            fontsize=16,
        )
    else:
        mesh = axis.pcolormesh(
            np.radians(longitude_edges_deg),
            np.radians(latitude_edges_deg),
            np.ma.masked_less_equal(values.T, 0),
            cmap=cmap,
            norm=norm,
            shading="auto",
            rasterized=True,
        )
        colorbar = figure.colorbar(
            mesh, ax=axis, orientation="horizontal", pad=0.09, shrink=0.78, aspect=40
        )
        colorbar.set_label(colorbar_label)

    axis.set_xticks(np.radians([-150, -120, -60, 0, 60, 120, 150]))
    axis.set_xticklabels(_longitude_tick_labels(coordinate_system))
    for tick_label in axis.get_xticklabels():
        tick_label.set_color("#e2e8f0")
        tick_label.set_fontweight("semibold")
    for tick_label in axis.get_yticklabels():
        tick_label.set_color("#1e293b")
    longitude_name = (
        "Right ascension" if coordinate_system == "equatorial" else "Galactic longitude"
    )
    latitude_name = "Declination" if coordinate_system == "equatorial" else "Galactic latitude"
    axis.set_xlabel(f"{longitude_name} (increases left)", labelpad=14)
    axis.set_ylabel(latitude_name, labelpad=10)
    figure.suptitle(title, fontsize=15, fontweight="bold", y=0.965)
    figure.text(
        0.5,
        0.90,
        f"Energy selection: {energy.label}  •  Selected events: {selected_events:,}\n"
        "Exploratory visualization — not exposure-corrected flux",
        fontsize=10,
        color="#475569",
        ha="center",
        va="top",
    )
    figure.savefig(output_path, format="png", facecolor="white")
    figure.clear()
    return output_path


def render_product_set(
    histograms: SkyHistograms,
    *,
    output_dir: Path,
    scope_title: str,
    energy: EnergyRange,
) -> list[Path]:
    products = (
        (
            histograms.counts_equatorial,
            "counts_equatorial.png",
            f"Fermi LAT photon counts — {scope_title} — Equatorial",
            "equatorial",
            "Selected photon events per sky bin",
            "viridis",
        ),
        (
            histograms.counts_galactic,
            "counts_galactic.png",
            f"Fermi LAT photon counts — {scope_title} — Galactic",
            "galactic",
            "Selected photon events per sky bin",
            "viridis",
        ),
        (
            histograms.energy_equatorial_mev,
            "energy_sum_equatorial.png",
            f"Fermi LAT summed photon energy — {scope_title} — Equatorial",
            "equatorial",
            "Summed selected photon energy per sky bin (MeV)",
            "cividis",
        ),
        (
            histograms.energy_galactic_mev,
            "energy_sum_galactic.png",
            f"Fermi LAT summed photon energy — {scope_title} — Galactic",
            "galactic",
            "Summed selected photon energy per sky bin (MeV)",
            "cividis",
        ),
    )
    return [
        render_sky_map(
            values,
            histograms.longitude_edges_deg,
            histograms.latitude_edges_deg,
            output_path=output_dir / filename,
            title=title,
            coordinate_system=coordinate_system,
            colorbar_label=colorbar_label,
            energy=energy,
            selected_events=histograms.selected_events,
            cmap=cmap,
        )
        for values, filename, title, coordinate_system, colorbar_label, cmap in products
    ]


PipelineProgress = Callable[[float, str], None]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso_time(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _save_histograms(path: Path, histograms: SkyHistograms) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as stream:
        np.savez_compressed(
            stream,
            longitude_edges_deg=histograms.longitude_edges_deg,
            latitude_edges_deg=histograms.latitude_edges_deg,
            counts_equatorial=histograms.counts_equatorial,
            counts_galactic=histograms.counts_galactic,
            energy_equatorial_mev=histograms.energy_equatorial_mev,
            energy_galactic_mev=histograms.energy_galactic_mev,
            input_events=np.int64(histograms.input_events),
            selected_events=np.int64(histograms.selected_events),
            selected_energy_mev=np.float64(histograms.selected_energy_mev),
        )
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _new_run_directory(config: RunConfig, started_at: datetime) -> Path:
    timestamp = started_at.strftime("%Y%m%dT%H%M%SZ")
    stem = f"run_{timestamp}_w{config.start_week:03d}-w{config.end_week:03d}"
    config.output_dir.mkdir(parents=True, exist_ok=True)
    candidate = config.output_dir / stem
    suffix = 2
    while candidate.exists():
        candidate = config.output_dir / f"{stem}_{suffix}"
        suffix += 1
    candidate.mkdir(parents=False)
    return candidate


def _cancel_if_requested(is_cancelled: CancelCheck | None) -> None:
    if is_cancelled and is_cancelled():
        raise ProcessingCancelled("Run cancelled.")


def run_pipeline(
    config: RunConfig,
    *,
    progress: PipelineProgress | None = None,
    is_cancelled: CancelCheck | None = None,
    downloader: WeeklyFileDownloader | None = None,
) -> PipelineResult:
    """Download, select, histogram, render, and document a complete run."""

    started_at = _utc_now()
    run_dir = _new_run_directory(config, started_at)
    manifest_path = run_dir / "manifest.json"
    image_paths: list[Path] = []
    combined = SkyHistograms.empty(config.bins)
    weeks = list(config.weeks)
    total_weeks = len(weeks)
    downloader = downloader or WeeklyFileDownloader()
    manifest: dict[str, Any] = {
        "application": "Fermi LAT Sky Explorer",
        "application_version": __version__,
        "status": "running",
        "started_at_utc": _iso_time(started_at),
        "scientific_scope": (
            "Exploratory photon-count and summed-energy visualization; not exposure-corrected flux."
        ),
        "config": config.as_dict(),
        "weeks": [],
        "output_files": [],
    }
    _atomic_json(manifest_path, manifest)

    def emit(fraction: float, message: str) -> None:
        bounded = min(1.0, max(0.0, fraction))
        LOGGER.info("%s", message)
        if progress:
            progress(bounded, message)

    try:
        for index, week in enumerate(weeks):
            _cancel_if_requested(is_cancelled)
            filename = config.filename_for_week(week)
            week_base = index * 0.9 / total_weeks
            week_span = 0.9 / total_weeks

            def download_progress(
                current: int,
                total: int | None,
                message: str,
                base: float = week_base,
                span: float = week_span,
            ) -> None:
                fraction = current / total if total and total > 0 else 0.0
                emit(base + span * 0.18 * fraction, message)

            fits_path = downloader.fetch(
                base_url=config.base_url,
                filename=filename,
                destination_dir=config.data_dir,
                force=config.force_download,
                progress=download_progress,
                is_cancelled=is_cancelled,
            )

            def process_progress(
                current: int,
                total: int,
                message: str,
                base: float = week_base,
                span: float = week_span,
            ) -> None:
                fraction = current / total if total > 0 else 1.0
                emit(base + span * (0.18 + 0.62 * fraction), message)

            histograms, week_metadata = process_weekly_fits(
                fits_path,
                config,
                progress=process_progress,
                is_cancelled=is_cancelled,
            )
            combined.add(histograms)
            week_metadata["week"] = week
            week_metadata["energy_selection_mev"] = config.energy.as_dict()
            week_metadata["max_zenith_angle_deg"] = config.max_zenith_angle_deg

            if config.create_per_week:
                _cancel_if_requested(is_cancelled)
                emit(week_base + week_span * 0.84, f"Rendering maps for week {week:03d}")
                week_dir = run_dir / f"week_{week:03d}"
                _save_histograms(week_dir / "histograms.npz", histograms)
                _atomic_json(week_dir / "metadata.json", week_metadata)
                image_paths.extend(
                    render_product_set(
                        histograms,
                        output_dir=week_dir,
                        scope_title=f"Week {week:03d}",
                        energy=config.energy,
                    )
                )

            manifest["weeks"].append(week_metadata)
            _atomic_json(manifest_path, manifest)
            emit(week_base + week_span, f"Completed week {week:03d}")

        _cancel_if_requested(is_cancelled)
        emit(0.92, "Rendering combined maps")
        combined_dir = run_dir / "combined"
        _save_histograms(combined_dir / "histograms.npz", combined)
        combined_scope = (
            f"Week {config.start_week:03d}"
            if config.start_week == config.end_week
            else f"Weeks {config.start_week:03d}–{config.end_week:03d}"
        )
        image_paths.extend(
            render_product_set(
                combined,
                output_dir=combined_dir,
                scope_title=combined_scope,
                energy=config.energy,
            )
        )

        completed_at = _utc_now()
        all_output_files = sorted(
            str(path.relative_to(run_dir)) for path in run_dir.rglob("*") if path.is_file()
        )
        manifest.update(
            {
                "status": "completed",
                "completed_at_utc": _iso_time(completed_at),
                "input_events": combined.input_events,
                "selected_events": combined.selected_events,
                "selected_energy_mev": combined.selected_energy_mev,
                "output_files": all_output_files,
            }
        )
        _atomic_json(manifest_path, manifest)
        emit(1.0, f"Completed. Results saved to {run_dir}")
        return PipelineResult(
            run_dir=run_dir,
            manifest_path=manifest_path,
            image_paths=tuple(image_paths),
            selected_events=combined.selected_events,
            selected_energy_mev=combined.selected_energy_mev,
        )
    except Exception as exc:
        manifest.update(
            {
                "status": "cancelled" if isinstance(exc, ProcessingCancelled) else "failed",
                "completed_at_utc": _iso_time(_utc_now()),
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }
        )
        try:
            _atomic_json(manifest_path, manifest)
        except OSError:
            LOGGER.exception("Could not update failure manifest at %s", manifest_path)
        raise


# ---------------------------------------------------------------------------
# Responsive desktop interface
# ---------------------------------------------------------------------------


STYLE_SHEET = """
QMainWindow, QWidget { background: #f7f8fc; color: #172033; font-size: 13px; }
QFrame#controlCard { background: white; border: 1px solid #dfe3ec; border-radius: 12px; }
QLabel#title { font-size: 24px; font-weight: 700; color: #101828; }
QLabel#subtitle { color: #5d6678; font-size: 12px; }
QLineEdit, QSpinBox, QDoubleSpinBox, QListWidget, QPlainTextEdit {
    background: white; border: 1px solid #cfd5e1; border-radius: 6px; padding: 6px;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QListWidget:focus {
    border: 1px solid #4f66e8;
}
QPushButton {
    background: #eef1f7; border: 1px solid #d5dbe7;
    border-radius: 7px; padding: 8px 12px;
}
QPushButton:hover { background: #e3e8f2; }
QPushButton:disabled { color: #99a1b2; background: #f3f4f7; }
QPushButton#primaryButton { background: #334bd6; color: white; border: none; font-weight: 600; }
QPushButton#primaryButton:hover { background: #263dbd; }
QPushButton#cancelButton { background: #fff1f0; color: #a32924; border-color: #f0c5c1; }
QProgressBar {
    border: 1px solid #d5dbe7; border-radius: 6px;
    background: white; text-align: center;
}
QProgressBar::chunk { background: #4f66e8; border-radius: 5px; }
QTabWidget::pane { border: 1px solid #dfe3ec; background: white; }
QTabBar::tab { background: #e9edf5; padding: 8px 16px; margin-right: 2px; }
QTabBar::tab:selected { background: white; font-weight: 600; }
"""


class AnalysisWorker(QObject):
    """Runs the pipeline off the GUI thread and reports safe Qt signals."""

    progress = Signal(float, str)
    finished = Signal(object)
    cancelled = Signal(str)
    failed = Signal(str, str)

    def __init__(self, config: RunConfig, cancellation: Event) -> None:
        super().__init__()
        self._config = config
        self._cancellation = cancellation

    @Slot()
    def run(self) -> None:
        try:
            result = run_pipeline(
                self._config,
                progress=self.progress.emit,
                is_cancelled=self._cancellation.is_set,
            )
            self.finished.emit(result)
        except ProcessingCancelled as exc:
            self.cancelled.emit(str(exc))
        except Exception as exc:
            self.failed.emit(str(exc), traceback.format_exc())


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Fermi LAT Sky Explorer")
        self.resize(1320, 820)
        self.setMinimumSize(1000, 680)
        self.setStyleSheet(STYLE_SHEET)

        self._thread: QThread | None = None
        self._worker: AnalysisWorker | None = None
        self._cancellation: Event | None = None
        self._result: PipelineResult | None = None
        self._preview_source: QPixmap | None = None
        self._close_when_finished = False
        self._configuration_widgets: list[QWidget] = []

        self._build_ui()
        self._set_running(False)

    def _build_ui(self) -> None:
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(20, 16, 20, 18)
        root_layout.setSpacing(12)

        title = QLabel("Fermi LAT Sky Explorer")
        title.setObjectName("title")
        subtitle = QLabel(
            "Public all-sky weekly photons • memory-bounded FITS processing • reproducible maps"
        )
        subtitle.setObjectName("subtitle")
        root_layout.addWidget(title)
        root_layout.addWidget(subtitle)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_controls())
        splitter.addWidget(self._build_results())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([360, 940])
        root_layout.addWidget(splitter, 1)
        self.setCentralWidget(root)

    def _build_controls(self) -> QWidget:
        card = QFrame()
        card.setObjectName("controlCard")
        card.setMinimumWidth(330)
        card.setMaximumWidth(430)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        heading = QLabel("Run configuration")
        heading.setStyleSheet("font-size: 17px; font-weight: 650;")
        layout.addWidget(heading)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.setVerticalSpacing(10)

        self.start_week = QSpinBox()
        self.start_week.setRange(0, 9999)
        self.start_week.setValue(9)
        self.end_week = QSpinBox()
        self.end_week.setRange(0, 9999)
        self.end_week.setValue(10)

        self.energy_min = QDoubleSpinBox()
        self.energy_min.setRange(0, 1_000_000_000)
        self.energy_min.setDecimals(1)
        self.energy_min.setValue(100.0)
        self.energy_min.setSuffix(" MeV")
        self.energy_max = QDoubleSpinBox()
        self.energy_max.setRange(0, 1_000_000_000)
        self.energy_max.setDecimals(1)
        self.energy_max.setValue(100_000.0)
        self.energy_max.setSuffix(" MeV")

        self.bins = QSpinBox()
        self.bins.setRange(36, 1440)
        self.bins.setSingleStep(36)
        self.bins.setValue(360)

        self.zenith_enabled = QCheckBox("Apply zenith-angle limit")
        self.zenith_limit = QDoubleSpinBox()
        self.zenith_limit.setRange(1, 180)
        self.zenith_limit.setValue(90)
        self.zenith_limit.setSuffix("°")
        self.zenith_limit.setEnabled(False)
        self.zenith_enabled.toggled.connect(self.zenith_limit.setEnabled)

        self.per_week = QCheckBox("Create maps for every week")
        self.per_week.setChecked(True)

        form.addRow("Start week", self.start_week)
        form.addRow("End week", self.end_week)
        form.addRow("Minimum energy", self.energy_min)
        form.addRow("Maximum energy", self.energy_max)
        form.addRow("Longitude bins", self.bins)
        form.addRow(self.zenith_enabled, self.zenith_limit)
        form.addRow("", self.per_week)
        layout.addLayout(form)

        self.data_dir = QLineEdit(str(DEFAULT_DATA_DIR.resolve()))
        self.output_dir = QLineEdit(str(DEFAULT_OUTPUT_DIR.resolve()))
        layout.addWidget(self._folder_row("FITS cache", self.data_dir, self._choose_data_dir))
        layout.addWidget(self._folder_row("Results", self.output_dir, self._choose_output_dir))

        scope_note = QLabel(
            "Maps show selected photon counts and summed event energy. They are not "
            "exposure-corrected flux products."
        )
        scope_note.setWordWrap(True)
        scope_note.setStyleSheet(
            "background: #fff8e7; border: 1px solid #f1dfad; border-radius: 7px; "
            "padding: 9px; color: #66521b;"
        )
        layout.addWidget(scope_note)

        button_row = QHBoxLayout()
        self.start_button = QPushButton("Start analysis")
        self.start_button.setObjectName("primaryButton")
        self.start_button.clicked.connect(self._start_analysis)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("cancelButton")
        self.cancel_button.clicked.connect(self._cancel_analysis)
        button_row.addWidget(self.start_button, 1)
        button_row.addWidget(self.cancel_button)
        layout.addLayout(button_row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.status_label = QLabel("Ready")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.status_label)

        self.open_folder_button = QPushButton("Open result folder")
        self.open_folder_button.clicked.connect(self._open_result_folder)
        layout.addWidget(self.open_folder_button)
        layout.addStretch(1)

        self._configuration_widgets.extend(
            [
                self.start_week,
                self.end_week,
                self.energy_min,
                self.energy_max,
                self.bins,
                self.zenith_enabled,
                self.zenith_limit,
                self.per_week,
                self.data_dir,
                self.output_dir,
            ]
        )
        return card

    def _folder_row(
        self, label_text: str, field: QLineEdit, callback: Callable[[], None]
    ) -> QWidget:
        container = QWidget()
        box = QVBoxLayout(container)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(4)
        label = QLabel(label_text)
        row = QHBoxLayout()
        browse = QPushButton("Browse")
        browse.clicked.connect(callback)
        row.addWidget(field, 1)
        row.addWidget(browse)
        box.addWidget(label)
        box.addLayout(row)
        self._configuration_widgets.append(browse)
        return container

    def _build_results(self) -> QWidget:
        tabs = QTabWidget()
        results = QWidget()
        results_layout = QHBoxLayout(results)
        results_layout.setContentsMargins(10, 10, 10, 10)
        self.image_list = QListWidget()
        self.image_list.setMinimumWidth(250)
        self.image_list.setMaximumWidth(340)
        self.image_list.currentRowChanged.connect(self._show_selected_image)
        results_layout.addWidget(self.image_list)

        self.preview_label = QLabel("Run an analysis to generate sky maps.")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.preview_label.setMinimumSize(480, 360)
        self.preview_label.setStyleSheet(
            "background: #111827; color: #aab4c8; border-radius: 8px;"
        )
        self.preview_scroll = QScrollArea()
        self.preview_scroll.setWidgetResizable(True)
        self.preview_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.preview_scroll.setWidget(self.preview_label)
        results_layout.addWidget(self.preview_scroll, 1)
        tabs.addTab(results, "Maps")

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText("Run progress and errors appear here.")
        tabs.addTab(self.log_view, "Run log")
        return tabs

    def _choose_data_dir(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Choose FITS cache", self.data_dir.text())
        if selected:
            self.data_dir.setText(selected)

    def _choose_output_dir(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self, "Choose results folder", self.output_dir.text()
        )
        if selected:
            self.output_dir.setText(selected)

    def _build_config(self) -> RunConfig:
        return RunConfig(
            start_week=self.start_week.value(),
            end_week=self.end_week.value(),
            energy=EnergyRange(self.energy_min.value(), self.energy_max.value()),
            bins=self.bins.value(),
            max_zenith_angle_deg=(
                self.zenith_limit.value() if self.zenith_enabled.isChecked() else None
            ),
            create_per_week=self.per_week.isChecked(),
            data_dir=Path(self.data_dir.text()),
            output_dir=Path(self.output_dir.text()),
        )

    def _start_analysis(self) -> None:
        try:
            config = self._build_config()
        except FermiSkyError as exc:
            QMessageBox.warning(self, "Invalid configuration", str(exc))
            return

        week_count = config.end_week - config.start_week + 1
        if week_count > 4:
            answer = QMessageBox.question(
                self,
                "Large download",
                f"This run requests {week_count} weekly files. Fermi weekly files can be "
                "tens to hundreds of megabytes each. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        self.image_list.clear()
        self.preview_label.setText("Analysis is running…")
        self.preview_label.setPixmap(QPixmap())
        self._preview_source = None
        self.log_view.clear()
        self._result = None
        self._cancellation = Event()
        self._thread = QThread(self)
        self._worker = AnalysisWorker(config, self._cancellation)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.cancelled.connect(self._on_cancelled)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.cancelled.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._on_thread_finished)
        self._thread.finished.connect(self._thread.deleteLater)
        self._set_running(True)
        self._thread.start()

    def _cancel_analysis(self) -> None:
        if self._cancellation:
            self._cancellation.set()
            self.cancel_button.setEnabled(False)
            self.status_label.setText("Cancellation requested; finishing the current safe step…")
            self.log_view.appendPlainText("Cancellation requested.")

    def _set_running(self, running: bool) -> None:
        for widget in getattr(self, "_configuration_widgets", []):
            widget.setEnabled(not running)
        if not running:
            self.zenith_limit.setEnabled(self.zenith_enabled.isChecked())
        self.start_button.setEnabled(not running)
        self.cancel_button.setEnabled(running)
        self.open_folder_button.setEnabled(not running and self._result is not None)
        if not running and self.progress_bar.value() < 100:
            self.progress_bar.setValue(0)

    @Slot(float, str)
    def _on_progress(self, fraction: float, message: str) -> None:
        self.progress_bar.setValue(round(fraction * 100))
        self.status_label.setText(message)
        self.log_view.appendPlainText(message)

    @Slot(object)
    def _on_finished(self, result: PipelineResult) -> None:
        self._result = result
        self.progress_bar.setValue(100)
        self.status_label.setText(
            f"Completed: {result.selected_events:,} selected events. Results: {result.run_dir}"
        )
        self.log_view.appendPlainText(f"Completed successfully: {result.run_dir}")
        for path in result.image_paths:
            item_label = str(path.relative_to(result.run_dir)).replace("\\", "/")
            self.image_list.addItem(item_label)
            self.image_list.item(self.image_list.count() - 1).setData(
                Qt.ItemDataRole.UserRole, str(path)
            )
        if self.image_list.count():
            self.image_list.setCurrentRow(0)

    @Slot(str)
    def _on_cancelled(self, message: str) -> None:
        self.status_label.setText(message or "Cancelled")
        self.log_view.appendPlainText(message or "Cancelled")

    @Slot(str, str)
    def _on_failed(self, message: str, details: str) -> None:
        LOGGER.error("Background analysis failed:\n%s", details)
        self.status_label.setText(f"Failed: {message}")
        self.log_view.appendPlainText(details)
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Critical)
        dialog.setWindowTitle("Analysis failed")
        dialog.setText(message)
        dialog.setDetailedText(details)
        dialog.exec()

    @Slot()
    def _on_thread_finished(self) -> None:
        self._thread = None
        self._worker = None
        self._cancellation = None
        self._set_running(False)
        if self._close_when_finished:
            QTimer.singleShot(0, self.close)

    @Slot(int)
    def _show_selected_image(self, row: int) -> None:
        if row < 0:
            return
        item = self.image_list.item(row)
        path = Path(item.data(Qt.ItemDataRole.UserRole))
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self.preview_label.setText(f"Could not load {path.name}")
            return
        self._preview_source = pixmap
        self._scale_preview()

    def _scale_preview(self) -> None:
        if not self._preview_source:
            return
        viewport_size = self.preview_scroll.viewport().size()
        target = viewport_size - QSize(24, 24)
        scaled = self._preview_source.scaled(
            target,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.preview_label.setPixmap(scaled)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._scale_preview()

    def _open_result_folder(self) -> None:
        if self._result:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._result.run_dir)))

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._thread and self._thread.isRunning():
            answer = QMessageBox.question(
                self,
                "Analysis is running",
                "Cancel the analysis and close after it reaches a safe stopping point?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._close_when_finished = True
                self._cancel_analysis()
            event.ignore()
            return
        event.accept()


def _exception_hook(exception_type: type[BaseException], exception: BaseException, tb: Any) -> None:
    LOGGER.critical("Unhandled GUI exception", exc_info=(exception_type, exception, tb))
    sys.__excepthook__(exception_type, exception, tb)


def run_gui() -> int:
    if QT_IMPORT_ERROR is not None:
        raise FermiSkyError(
            "The desktop interface could not start because the Qt runtime is unavailable: "
            f"{QT_IMPORT_ERROR}. Run setup_windows.bat to repair the isolated environment."
        )
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationDisplayName("Fermi LAT Sky Explorer")
    app.setOrganizationName("Fermi LAT Sky Explorer")
    app.setStyle("Fusion")
    sys.excepthook = _exception_hook
    window = MainWindow()
    window.show()
    return app.exec()


# ---------------------------------------------------------------------------
# CLI and local dependency check
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fermi-sky",
        description="Explore Fermi LAT all-sky weekly photon FITS files.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    gui_parser = subparsers.add_parser("gui", help="Start the desktop interface.")
    gui_parser.add_argument("--log-level", default=DEFAULT_LOG_LEVEL)

    run_parser = subparsers.add_parser("run", help="Run analysis without the GUI.")
    run_parser.add_argument("--start-week", type=int, required=True)
    run_parser.add_argument("--end-week", type=int, required=True)
    run_parser.add_argument("--energy-min", type=float, default=100.0, metavar="MEV")
    run_parser.add_argument("--energy-max", type=float, default=100_000.0, metavar="MEV")
    run_parser.add_argument(
        "--no-energy-filter",
        action="store_true",
        help="Use all finite positive energies in the input file.",
    )
    run_parser.add_argument("--bins", type=int, default=360)
    run_parser.add_argument("--chunk-rows", type=int, default=500_000)
    run_parser.add_argument("--max-zenith-angle", type=float, default=None, metavar="DEGREES")
    run_parser.add_argument("--combined-only", action="store_true")
    run_parser.add_argument("--force-download", action="store_true")
    run_parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    run_parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    run_parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    run_parser.add_argument("--file-pattern", default=DEFAULT_FILE_PATTERN)
    run_parser.add_argument("--log-level", default=DEFAULT_LOG_LEVEL)

    doctor_parser = subparsers.add_parser(
        "doctor", help="Check Python and application dependencies."
    )
    doctor_parser.add_argument("--log-level", default=DEFAULT_LOG_LEVEL)
    return parser


def _run_command(args: argparse.Namespace) -> int:
    energy = (
        EnergyRange(None, None)
        if args.no_energy_filter
        else EnergyRange(args.energy_min, args.energy_max)
    )
    config = RunConfig(
        start_week=args.start_week,
        end_week=args.end_week,
        energy=energy,
        bins=args.bins,
        chunk_rows=args.chunk_rows,
        max_zenith_angle_deg=args.max_zenith_angle,
        create_per_week=not args.combined_only,
        force_download=args.force_download,
        data_dir=args.data_dir,
        output_dir=args.output_dir,
        base_url=args.base_url,
        file_pattern=args.file_pattern,
    )
    last_percent = -1

    def show_progress(fraction: float, message: str) -> None:
        nonlocal last_percent
        percent = int(fraction * 100)
        if percent != last_percent:
            print(f"[{percent:3d}%] {message}", flush=True)
            last_percent = percent

    result = run_pipeline(config, progress=show_progress)
    print()
    print(f"Results: {result.run_dir}")
    print(f"Selected events: {result.selected_events:,}")
    print(f"Selected energy: {result.selected_energy_mev:,.3f} MeV")
    return 0


def _doctor() -> int:
    versions = {
        "Python": sys.version.split()[0],
        "NumPy": np.__version__,
        "Astropy": __import__("astropy").__version__,
        "Matplotlib": __import__("matplotlib").__version__,
        "PySide6": importlib.metadata.version("PySide6"),
    }
    print(f"Fermi LAT Sky Explorer {__version__}")
    for name, version in versions.items():
        print(f"{name}: {version}")
    if sys.version_info < (3, 10):
        print("Error: Python 3.10 or newer is required.", file=sys.stderr)
        return 2
    if QT_IMPORT_ERROR is not None:
        print(f"Error: the Qt desktop runtime could not load: {QT_IMPORT_ERROR}", file=sys.stderr)
        return 2
    EnergyRange(100.0, 100_000.0)
    SkyHistograms.empty(36)
    print("Status: ready")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        arguments = ["gui"]
    parser = _parser()
    args = parser.parse_args(arguments)
    configure_logging(args.log_level)

    try:
        if args.command == "gui":
            return run_gui()
        if args.command == "doctor":
            return _doctor()
        return _run_command(args)
    except FermiSkyError as exc:
        LOGGER.error("%s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        LOGGER.warning("Interrupted by user")
        print("Cancelled.", file=sys.stderr)
        return 130
    except Exception as exc:
        LOGGER.exception("Unexpected application failure")
        print(f"Unexpected error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
