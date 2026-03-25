"""
================================================================================
  INTELLIGENT FPGA SUPERVISORY CONTROL SYSTEM  —  FINAL VERSION
  100 Gbps WDM Photonics | EDFA Gain + MZM Bias Adaptive Optimization
================================================================================
Version      : FINAL (v4.0)
Project      : 100 Gbps WDM Photonics System with FPGA Supervisory Control
               4 × 25 Gbps NRZ | 193.1–193.4 THz | 387 km SMF+DCF
Simulation   : OptiSystem Version 23.00
Interface    : Python → OptiSystem via COM (win32com)

WHAT THIS SCRIPT DOES (plain English):
────────────────────────────────────────────────────────────────────────────────
1. Connects to OptiSystem and reads the Q-factor from BER Analyzer_3
2. Optimises EDFA gain across all 4 spans using adaptive momentum-gradient
3. Injects a known thermal drift (+0.6 V) into the MZM bias
4. Recovers the correct MZM bias using momentum-gradient + operating-point logic
5. Monitors for drift using ANN-enhanced prediction
6. Saves a 9-panel publication dashboard + structured session logs

ANDERSON (2023) RESEARCH ENHANCEMENTS — All correctly implemented:
────────────────────────────────────────────────────────────────────────────────
[A] EWMA Smoothing           Q_ewma = α·Q_new + (1−α)·Q_prev   [α=0.3]
    IQR outlier rejection applied first, then EWMA. Both stages documented.
    Reference: Anderson (2023) Section 4.4 — filter output averaging.

[B] MZM Transfer Model       T(V) = ½[1 + cos(π·V/Vπ)]    [Vπ = 4.0 V]
    Dedicated MZMTransferModel class encapsulates all transfer-curve logic.
    Reference: Anderson (2023) Equation 4.1.

[C] Analytical Harmonic Ratio
    Fund  ∝ |sin(πV/Vπ)| × J₁(πVac/Vπ)   [Bessel J₁]
    Harm2 ∝ |cos(πV/Vπ)| × J₂(πVac/Vπ)   [Bessel J₂]
    Ratio → ∞ at quadrature, → 0 at peak/null.
    Simulation-domain equivalent of Anderson's Goertzel FPGA filter.
    Reference: Anderson (2023) Equations 4.4 and 4.5.

[D] Operating Point Classifier
    NEAR_PEAK | NEAR_QUADRATURE | NEAR_NULL | UNKNOWN
    Region-aware step multipliers: larger steps far from quadrature.
    Reference: Anderson (2023) Section 4.2.

[E] Vπ-Normalised Bias Reporting
    All corrections expressed as V/Vπ — modulator-independent fraction.
    Reference: Anderson (2023) Section 6.3.

[F] ANN Drift Predictor      [Anderson 2023, Section 6.3]
    sklearn MLPRegressor: 5-sample window → Dense(8,ReLU) → Dense(8,ReLU) → Q̂
    Online training on accumulated Q history during each session.
    StandardScaler normalisation for robust neural network inputs.
    Falls back to linear regression when < min_samples available.
    Prediction log stored for dashboard plotting.

[G] Operation Mode Selector  [Anderson 2023, Ch.3 — Repurposable Hardware]
    CALIBRATION : Full EDFA sweep + MZM recovery + drift check
    OPERATIONAL : MZM correction only (EDFA already calibrated)
    MONITORING  : Read-only Q logging and drift prediction, no actuation

[H] 9-Panel Publication Dashboard (3×3)
    Including: ANN Predicted vs Actual Q, Harmonic Ratio vs Bias
    with theoretical curve overlay, Operating Point bar-chart timeline.

CRITICAL — OptiSystem COM API facts (DO NOT CHANGE):
────────────────────────────────────────────────────────────────────────────────
  doc.CalculateProject(True, True) is SYNCHRONOUS and BLOCKING.
  Python waits at that line until OptiSystem finishes completely.
  IsBusy() does NOT exist in the OptiSystem COM API — do not add it.
  time.sleep(1.0) after CalculateProject allows COM objects to settle.
  SetComponentParameterValue("", 1, name, "Gain", value) — sets EDFA gain.
  SetParameterValue("Bias voltage1", v) — sets MZM V1.
  GetComponentResultValue("", 1, "BER Analyzer_3", "Max. Q Factor") — reads Q.
================================================================================
"""

# ─────────────────────────────────────────────────────────────────────────────
# IMPORTS
# ─────────────────────────────────────────────────────────────────────────────
import json
import logging
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import matplotlib.patches as mpatches
from scipy import stats, special          # special.jv for Bessel functions
from scipy.signal import savgol_filter

# ── Optional: OptiSystem COM bridge ──────────────────────────────────────────
try:
    import win32com.client
    WIN32_AVAILABLE = True
except ImportError:
    WIN32_AVAILABLE = False
    print("[WARNING] win32com not available — VIRTUAL SIMULATION MODE active")

# ── Optional: scikit-learn for ANN Drift Predictor ───────────────────────────
try:
    from sklearn.neural_network import MLPRegressor
    from sklearn.preprocessing import StandardScaler
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False
    print("[WARNING] scikit-learn not available — ANN predictor disabled, "
          "using linear regression fallback")


# ══════════════════════════════════════════════════════════════════════════════
# 1.  OPERATION MODE  [Anderson 2023, Ch.3 — Repurposable Hardware]
# ══════════════════════════════════════════════════════════════════════════════

class OperationMode(Enum):
    """
    Three operational modes mirror Anderson's (2023) repurposable-hardware
    concept at the software supervisory level.

    Anderson argued that the same FPGA hardware should serve manufacturing
    calibration AND in-service operational control without redesign.
    This class implements the equivalent concept in Python:

      CALIBRATION : Full session — EDFA gain sweep + MZM drift recovery
                    + drift prediction check.
                    Use: system startup, first run, after maintenance.

      OPERATIONAL : Skip EDFA (gain already optimal from prior calibration).
                    Run MZM drift correction only.
                    Use: routine in-service thermal drift recovery.

      MONITORING  : Read-only. Collect Q readings and run drift predictor.
                    No parameter changes — no actuation.
                    Use: passive health monitoring of a live running system.

    Publication citation:
      "Following the repurposable hardware paradigm proposed by Anderson (2023),
       the supervisory control architecture operates in three distinct modes —
       CALIBRATION, OPERATIONAL, and MONITORING — without structural modification
       to the control algorithm, mirroring Anderson's reprogrammable FPGA concept
       at the software supervisory level."
    """
    CALIBRATION = "CALIBRATION"
    OPERATIONAL = "OPERATIONAL"
    MONITORING  = "MONITORING"


# ══════════════════════════════════════════════════════════════════════════════
# 2.  SYSTEM CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

class SystemConfig:
    """
    Loads all parameters from config.json.
    Falls back to built-in defaults if file is not found.

    CRITICAL: vpi_voltage MUST match the Vπ parameter of the MZM
    component in your OptiSystem project. For this project: Vπ = 4.0 V.
    This value is confirmed in the project specification (Chapter 8).
    """
    DEFAULT_CONFIG_PATH = Path("config.json")

    def __init__(self, config_path: Optional[Path] = None):
        path = config_path or self.DEFAULT_CONFIG_PATH
        if path.exists():
            with open(path, "r") as f:
                self._cfg = json.load(f)
            print(f"[Config] Loaded from {path}")
        else:
            self._cfg = self._default_config()
            print("[Config] config.json not found — using built-in defaults")

    def _default_config(self) -> dict:
        return {
            "operation_mode": "CALIBRATION",

            "optisystem": {
                "ber_analyzer_name": "BER Analyzer_3",
                "mzm_name":          "MZM",
                "edfa_names":        ["EDFA", "EDFA_1", "EDFA_2", "EDFA_3"]
            },

            "edfa_optimizer": {
                "initial_gain_db":       24.0,
                "gain_min_db":           18.0,
                "gain_max_db":           30.0,
                "initial_step_db":        0.5,
                "step_min_db":            0.01,
                "step_max_db":            1.0,
                "max_iterations":        30,
                "convergence_tolerance":  0.02,
                "momentum_beta":          0.7,
                "oscillation_window":     4,
                "smoothing_window":       3
            },

            "mzm_controller": {
                "drift_injection_voltage": 0.6,
                "initial_step_size":       0.1,
                "step_min":                0.005,
                "step_max":                0.3,
                "tolerance":               0.01,
                "max_iterations":          20,
                "bias_min_v":             -2.0,
                "bias_max_v":              2.0,
                "momentum_beta":           0.6,
                "smoothing_window":        3
            },

            "supervisory": {
                "q_target_minimum":               6.0,
                "q_excellent_threshold":          7.2,
                "instability_std_threshold":      0.3,
                "drift_detection_slope_threshold": -0.05,
                "recalibration_q_drop_threshold":  0.5,
                "state_history_window":            8
            },

            # ── Anderson (2023) enhancements ──────────────────────────────────
            "mzm_transfer_model": {
                "vpi_voltage":                      4.0,   # Vπ = 4.0 V (project spec)
                "vac_amplitude":                    0.08,  # pilot-tone Vac ≈ 2% of Vπ
                "ewma_alpha":                       0.3,   # EWMA weight on newest sample
                "near_peak_threshold_fraction":     0.20,
                "near_quadrature_threshold_fraction": 0.15,
                "near_null_threshold_fraction":     0.15
            },

            "ann_drift_predictor": {
                "enabled":               True,
                "window_size":           5,
                "min_training_samples":  10,   # reduced from 15 to suit session length
                "hidden_layer_sizes":    [8, 8],
                "max_iter":              500,
                "alpha_regularization":  0.001
            },

            "logging": {
                "log_dir":       "logs",
                "log_file":      "fpga_control_session.log",
                "console_level": "INFO",
                "file_level":    "DEBUG"
            }
        }

    def get(self, *keys):
        """Navigate nested config: cfg.get('section', 'key', 'subkey', ...)"""
        val = self._cfg
        for k in keys:
            val = val[k]
        return val

    def get_operation_mode(self) -> OperationMode:
        try:
            return OperationMode(self._cfg.get("operation_mode", "CALIBRATION"))
        except ValueError:
            return OperationMode.CALIBRATION


# ══════════════════════════════════════════════════════════════════════════════
# 3.  SYSTEM LOGGER
# ══════════════════════════════════════════════════════════════════════════════

class SystemLogger:
    """
    Dual-sink logger.
    Console → INFO  (clean real-time output).
    File    → DEBUG (full trace for reproducibility and submission).
    """
    def __init__(self, cfg: SystemConfig):
        log_dir  = Path(cfg.get("logging", "log_dir"))
        log_dir.mkdir(exist_ok=True)
        log_file = log_dir / cfg.get("logging", "log_file")

        self._logger = logging.getLogger("FPGAControlFINAL")
        self._logger.setLevel(logging.DEBUG)

        fh = logging.FileHandler(log_file, mode="a", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
        ))
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)
        ch.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%H:%M:%S"
        ))
        if not self._logger.handlers:
            self._logger.addHandler(fh)
            self._logger.addHandler(ch)

    def info(self, msg: str):    self._logger.info(msg)
    def debug(self, msg: str):   self._logger.debug(msg)
    def warning(self, msg: str): self._logger.warning(msg)
    def error(self, msg: str):   self._logger.error(msg)

    def section(self, title: str):
        bar = "=" * 60
        self._logger.info(f"\n{bar}\n  {title}\n{bar}")


# ══════════════════════════════════════════════════════════════════════════════
# 4.  OPTISYSTEM INTERFACE
# ══════════════════════════════════════════════════════════════════════════════

class SimulationMode(Enum):
    REAL    = auto()   # Live OptiSystem via COM
    VIRTUAL = auto()   # Built-in physics model (no OptiSystem needed)


class OptiSystemInterface:
    """
    Isolated COM bridge to OptiSystem.

    CONFIRMED from OptiSystem SDK documentation:
    ────────────────────────────────────────────────────────────────────────
    doc.CalculateProject(True, True) is SYNCHRONOUS and BLOCKING.
    Python waits at this line until OptiSystem finishes completely.
    OptiSystem does NOT have an IsBusy() method — do not add polling.
    time.sleep(1.0) after CalculateProject allows COM objects to settle.
    ────────────────────────────────────────────────────────────────────────

    Virtual mode physics:
      Q_gain = 9.5 × exp(−(G−25)² / 18)     — Gaussian EDFA response
      Q_bias = cos²(πV / 2Vπ)               — MZM transfer function
      Q      = Q_gain × Q_bias + noise        — combined response

      Vπ is read from config (mzm_transfer_model.vpi_voltage = 4.0 V)
      so virtual mode is always consistent with the transfer model.
    """

    def __init__(self, cfg: SystemConfig, logger: SystemLogger,
                 mode: SimulationMode = SimulationMode.REAL):
        self._cfg   = cfg
        self._log   = logger
        self._mode  = mode
        self._app   = None
        self._doc   = None
        self._mzm   = None

        # Virtual mode state
        self._sim_gain  = cfg.get("edfa_optimizer", "initial_gain_db")
        self._sim_bias  = 0.0
        self._sim_noise = 0.0
        # Read Vπ from config so virtual + transfer model are ALWAYS consistent
        self._vpi = cfg.get("mzm_transfer_model", "vpi_voltage")

        if mode == SimulationMode.REAL:
            self._connect()

    def _connect(self):
        """Connect to the currently open OptiSystem document via COM."""
        self._log.info("Connecting to OptiSystem via COM...")
        self._app = win32com.client.Dispatch("OptiSystem.Application")
        self._doc = self._app.GetActiveDocument()
        layout_mgr = self._doc.GetLayoutMgr()
        layout     = layout_mgr.GetCurrentLayout()
        canvas     = layout.GetCurrentCanvas()
        mzm_name   = self._cfg.get("optisystem", "mzm_name")
        self._mzm  = canvas.GetComponentByName(mzm_name)
        self._log.info("OptiSystem connection established.")

    def run_simulation(self) -> None:
        """
        Run OptiSystem simulation.
        CalculateProject(True,True) BLOCKS — Python waits here until done.
        sleep(1.0) is the settle time for COM result objects — do not remove.
        """
        if self._mode == SimulationMode.VIRTUAL:
            return
        self._doc.CalculateProject(True, True)
        time.sleep(1.0)

    def read_q_factor(self) -> float:
        """Read Max. Q Factor from BER Analyzer component."""
        if self._mode == SimulationMode.VIRTUAL:
            return self._compute_virtual_q()
        ber_name = self._cfg.get("optisystem", "ber_analyzer_name")
        return float(self._doc.GetComponentResultValue(
            "", 1, ber_name, "Max. Q Factor"
        ))

    def _compute_virtual_q(self) -> float:
        """
        Virtual physics model. Uses same Vπ as MZMTransferModel so
        virtual-mode behaviour is always physically consistent.
        """
        g     = self._sim_gain
        b     = self._sim_bias
        noise = np.random.normal(0, 0.05)
        q_gain = 9.5 * np.exp(-((g - 25.0) ** 2) / (2 * 3.0 ** 2))
        q_bias = np.cos(np.pi * b / (2.0 * self._vpi)) ** 2
        return float(max((q_gain * q_bias) + noise + self._sim_noise, 0.1))

    def set_edfa_gain(self, gain_db: float) -> None:
        """Apply gain_db to all EDFA spans in the layout."""
        if self._mode == SimulationMode.VIRTUAL:
            self._sim_gain = gain_db;  return
        for name in self._cfg.get("optisystem", "edfa_names"):
            self._doc.SetComponentParameterValue("", 1, name, "Gain", gain_db)

    def set_mzm_bias(self, bias_v: float) -> None:
        """Set MZM push-pull bias: V1 = +bias_v, V2 = −bias_v."""
        if self._mode == SimulationMode.VIRTUAL:
            self._sim_bias = bias_v;  return
        self._mzm.SetParameterValue("Bias voltage1",  bias_v)
        self._mzm.SetParameterValue("Bias voltage2", -bias_v)

    def inject_thermal_drift(self, drift_v: float) -> None:
        """Inject a known thermal drift offset to simulate MZM bias drift."""
        if self._mode == SimulationMode.VIRTUAL:
            self._sim_noise = -abs(drift_v) * 0.5
            self._sim_bias  = drift_v;  return
        self._mzm.SetParameterValue("Bias voltage1",  drift_v)
        self._mzm.SetParameterValue("Bias voltage2", -drift_v)


# ══════════════════════════════════════════════════════════════════════════════
# 5.  MZM TRANSFER FUNCTION MODEL  [Anderson 2023, Sec 4.2 + Eq 4.1/4.4/4.5]
# ══════════════════════════════════════════════════════════════════════════════

class OperatingPoint(Enum):
    """
    MZM transfer curve operating regions.
    Anderson (2023) Section 4.2 defines these three key points explicitly.
    """
    NEAR_PEAK        = "NEAR_PEAK"        # V ≈ 0 V    — maximum transmission
    NEAR_QUADRATURE  = "NEAR_QUADRATURE"  # V ≈ Vπ/2   — linear region, optimal
    NEAR_NULL        = "NEAR_NULL"        # V ≈ Vπ     — minimum transmission
    UNKNOWN          = "UNKNOWN"          # transition between defined regions


class MZMTransferModel:
    """
    Complete MZM transfer function model encapsulating all physics.

    This class is the dedicated owner of all Anderson-inspired transfer-curve
    calculations. All other modules call into this class — no module
    duplicates transfer-curve maths.

    CRITICAL — Vπ for this project:
    ────────────────────────────────────────────────────────────────────────
    The MZM switching voltage (Vπ) = 4.0 V as specified in the project
    documentation (Chapter 8, Table 8.1). This is the value at which the
    MZM output transitions from peak transmission to null transmission.
    All normalised bias values (V/Vπ) are computed using this value.
    Do NOT use 1.0 V — that would give operating point classifications and
    normalised corrections that are 4× wrong.
    ────────────────────────────────────────────────────────────────────────
    """

    def __init__(self, cfg: SystemConfig, logger: SystemLogger):
        self._log = logger
        sec = "mzm_transfer_model"
        self._vpi      = float(cfg.get(sec, "vpi_voltage"))       # = 4.0 V
        self._vac      = float(cfg.get(sec, "vac_amplitude"))     # pilot tone Vac
        self._thr_peak = float(cfg.get(sec, "near_peak_threshold_fraction"))
        self._thr_quad = float(cfg.get(sec, "near_quadrature_threshold_fraction"))
        self._thr_null = float(cfg.get(sec, "near_null_threshold_fraction"))
        self._log.debug(
            f"MZMTransferModel: Vπ={self._vpi} V  Vac={self._vac} V  "
            f"thresholds: peak<{self._thr_peak} | quad±{self._thr_quad} | "
            f"null±{self._thr_null}"
        )

    def transmission(self, bias_v: float) -> float:
        """
        Normalised optical power transmission [Anderson 2023, Eq 4.1].
        T(V) = ½ × [1 + cos(π·V / Vπ)]
        T = 1.0 at V=0 (peak), T = 0.5 at V=Vπ/2 (quadrature), T = 0 at V=Vπ (null).
        """
        return 0.5 * (1.0 + np.cos(np.pi * bias_v / self._vpi))

    def classify_operating_point(self, bias_v: float) -> OperatingPoint:
        """
        Classify which region of the MZM transfer curve the bias is in.
        [Anderson 2023, Section 4.2 — Key Operating Points]

        Uses V_norm = |V| / Vπ:
          NEAR_PEAK:        V_norm < thr_peak            (near V = 0)
          NEAR_QUADRATURE:  |V_norm − 0.5| < thr_quad   (near V = Vπ/2 = 2.0 V)
          NEAR_NULL:        |V_norm − 1.0| < thr_null   (near V = Vπ  = 4.0 V)
          UNKNOWN:          transition / between regions

        For this project at drift V = 0.6 V, Vπ = 4.0 V:
          V_norm = 0.6/4.0 = 0.15 → NEAR_PEAK (V_norm < 0.20 threshold)
        """
        v_norm = abs(bias_v) / self._vpi
        if v_norm < self._thr_peak:
            return OperatingPoint.NEAR_PEAK
        elif abs(v_norm - 0.5) < self._thr_quad:
            return OperatingPoint.NEAR_QUADRATURE
        elif abs(v_norm - 1.0) < self._thr_null:
            return OperatingPoint.NEAR_NULL
        else:
            return OperatingPoint.UNKNOWN

    def harmonic_ratio(self, bias_v: float) -> float:
        """
        Analytical fundamental-to-2nd-harmonic power ratio.
        [Anderson 2023, Equations 4.4 and 4.5]

        Derived from Bessel function expansion of MZM output:
          φ_DC = π · V_DC / Vπ          (DC phase)
          m    = π · V_AC / Vπ          (modulation index)

          P_fundamental  ∝ |sin(φ_DC)| · |J₁(m)|
          P_2nd_harmonic ∝ |cos(φ_DC)| · |J₂(m)|
          Harmonic Ratio = P_fundamental / P_2nd_harmonic

        Physical interpretation:
          At quadrature (φ_DC = π/2): sin=1, cos=0 → ratio → ∞ (capped 100)
          At peak       (φ_DC = 0  ): sin=0, cos=1 → ratio = 0
          At null       (φ_DC = π  ): sin=0, cos=1 → ratio = 0

        This is the simulation-domain equivalent of Anderson's Goertzel FPGA
        filter monitoring a real pilot tone at 4 kHz.
        """
        phi  = np.pi * bias_v   / self._vpi
        m    = np.pi * self._vac / self._vpi
        fund = abs(np.sin(phi)) * abs(float(special.jv(1, m)))
        sec2 = abs(np.cos(phi)) * abs(float(special.jv(2, m)))
        if sec2 < 1e-12:
            return 100.0
        return float(min(fund / sec2, 100.0))

    def normalize_bias(self, bias_v: float) -> float:
        """
        Express bias voltage as dimensionless fraction of Vπ. [Anderson 2023, Sec 6.3]
        result = V / Vπ
        Makes the algorithm modulator-independent (same number for any MZM type).
        """
        return bias_v / self._vpi if self._vpi != 0.0 else 0.0

    def step_multiplier(self, op: OperatingPoint) -> float:
        """
        Region-aware step size multiplier for MZM recovery.

        Anderson (2023) observed that dT/dV (transfer-curve sensitivity) varies
        across operating regions. A controller that is aware of its position
        can apply proportionally larger steps when far from the target and
        finer steps when near it, reducing overshoot and wasted iterations.

          NEAR_NULL      : 1.8× — far from quadrature, apply large correction
          UNKNOWN        : 1.2× — uncertain position, modest increase
          NEAR_PEAK      : 1.0× — close to optimal, standard step
          NEAR_QUADRATURE: 0.6× — at or near optimal, fine-tune only
        """
        return {
            OperatingPoint.NEAR_NULL:       1.8,
            OperatingPoint.UNKNOWN:         1.2,
            OperatingPoint.NEAR_PEAK:       1.0,
            OperatingPoint.NEAR_QUADRATURE: 0.6,
        }.get(op, 1.0)

    @property
    def vpi(self) -> float:
        return self._vpi


# ══════════════════════════════════════════════════════════════════════════════
# 6.  SIGNAL MONITOR  (IQR Outlier Rejection + EWMA)  [Anderson 2023, Sec 4.4]
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class SignalSample:
    timestamp:  float
    raw_q:      float
    ewma_q:     float = 0.0
    is_outlier: bool  = False


class SignalMonitor:
    """
    Statistically intelligent Q-factor monitor with two-stage smoothing.

    Stage 1 — IQR Outlier Rejection:
      If raw_q falls outside [Q1 − 1.5·IQR, Q3 + 1.5·IQR] of the recent
      window, it is replaced with the window mean. This prevents noise spikes
      from corrupting control decisions.

    Stage 2 — EWMA Smoothing [Anderson 2023, Section 4.4]:
      Q_ewma(t) = α · Q_clean(t) + (1 − α) · Q_ewma(t−1)
      α = 0.3: recent measurement weighted 30%, history 70%.
      Anderson recorded 100 samples per bias step and applied EWMA to reduce
      measurement noise in experimental hardware. The same approach is applied
      here to Q readings from OptiSystem.

    Control decisions are ALWAYS made on ewma_q — never on raw_q.
    Raw values are retained for comparison in the dashboard.
    """

    def __init__(self, cfg: SystemConfig, logger: SystemLogger,
                 optisys: OptiSystemInterface):
        self._cfg     = cfg
        self._log     = logger
        self._optisys = optisys

        win = cfg.get("edfa_optimizer", "smoothing_window")
        self._window: Deque[SignalSample] = deque(maxlen=max(win, 3))
        self._all_samples: List[SignalSample] = []

        self._ewma_alpha: float         = float(cfg.get("mzm_transfer_model",
                                                         "ewma_alpha"))
        self._ewma_q: Optional[float]   = None   # uninitialised until first read

    def measure(self) -> Tuple[float, float]:
        """
        Run simulation, apply IQR outlier rejection, then EWMA smoothing.
        Returns (raw_q, ewma_q).
        All downstream control uses ewma_q.
        """
        self._optisys.run_simulation()
        raw_q  = self._optisys.read_q_factor()
        sample = SignalSample(timestamp=time.time(), raw_q=raw_q)

        # ── Stage 1: IQR outlier rejection ───────────────────────────────────
        if len(self._window) >= 3:
            recent = [s.raw_q for s in self._window]
            q1, q3 = np.percentile(recent, [25, 75])
            iqr    = q3 - q1
            if raw_q < (q1 - 1.5 * iqr) or raw_q > (q3 + 1.5 * iqr):
                sample.is_outlier = True
                raw_q             = float(np.mean(recent))
                sample.raw_q      = raw_q
                self._log.debug(f"IQR outlier → replaced with mean={raw_q:.4f}")

        # ── Stage 2: EWMA smoothing [Anderson 2023, Sec 4.4] ────────────────
        if self._ewma_q is None:
            self._ewma_q = raw_q   # bootstrap: first clean measurement
        else:
            self._ewma_q = (self._ewma_alpha * raw_q
                            + (1.0 - self._ewma_alpha) * self._ewma_q)

        sample.ewma_q = self._ewma_q
        self._window.append(sample)
        self._all_samples.append(sample)

        self._log.debug(
            f"Q raw={sample.raw_q:.4f}  EWMA={self._ewma_q:.4f}  "
            f"α={self._ewma_alpha}  outlier={sample.is_outlier}"
        )
        return sample.raw_q, self._ewma_q

    def recent_std(self) -> float:
        vals = [s.ewma_q for s in self._window]
        return float(np.std(vals)) if len(vals) > 1 else 0.0

    def recent_mean(self) -> float:
        vals = [s.ewma_q for s in self._window]
        return float(np.mean(vals)) if vals else 0.0

    def all_smoothed(self) -> List[float]:
        return [s.ewma_q for s in self._all_samples]

    def all_raw(self) -> List[float]:
        return [s.raw_q for s in self._all_samples]


# ══════════════════════════════════════════════════════════════════════════════
# 7.  LINEAR REGRESSION DRIFT PREDICTOR  (baseline / fallback)
# ══════════════════════════════════════════════════════════════════════════════

class DriftPredictor:
    """
    Linear regression Q-trend analyser. Used as fallback by ANNDriftPredictor
    when insufficient data or scikit-learn is unavailable.

    Fits Q(t) = slope·t + intercept over Q history.
    Detects drift when slope < threshold AND R² > 0.5 (statistically significant).
    """
    def __init__(self, cfg: SystemConfig, logger: SystemLogger):
        self._cfg             = cfg
        self._log             = logger
        self._slope_threshold = cfg.get("supervisory",
                                        "drift_detection_slope_threshold")

    def analyse(self, q_history: List[float]) -> Dict:
        if len(q_history) < 4:
            return {"slope": 0.0, "r_squared": 0.0, "drift_detected": False,
                    "predicted_next": q_history[-1] if q_history else 0.0,
                    "ann_used": False}
        x = np.arange(len(q_history), dtype=float)
        y = np.array(q_history, dtype=float)
        slope, intercept, r_value, _, _ = stats.linregress(x, y)
        r_squared      = r_value ** 2
        predicted_next = float(slope * len(q_history) + intercept)
        drift_detected = (slope < self._slope_threshold) and (r_squared > 0.5)
        if drift_detected:
            self._log.warning(
                f"[LinReg] DRIFT: slope={slope:.4f}/step  "
                f"R²={r_squared:.3f}  pred_Q={predicted_next:.3f}"
            )
        else:
            self._log.debug(f"[LinReg] slope={slope:.4f}  R²={r_squared:.3f} OK")
        return {"slope": float(slope), "r_squared": float(r_squared),
                "drift_detected": drift_detected, "predicted_next": predicted_next,
                "ann_used": False}


# ══════════════════════════════════════════════════════════════════════════════
# 8.  ANN DRIFT PREDICTOR  [Anderson 2023, Section 6.3]
# ══════════════════════════════════════════════════════════════════════════════

class ANNDriftPredictor(DriftPredictor):
    """
    Anderson (2023) inspired lightweight feedforward ANN for Q-factor drift
    prediction, extending the linear regression baseline.

    Architecture [adapted from Anderson Sec 6.3, Table 6-3]:
      Input:          window_size most recent Q values (default 5)
      Hidden layer 1: 8 neurons, ReLU activation
      Hidden layer 2: 8 neurons, ReLU activation
      Output:         1 neuron — predicted next Q value

    Anderson demonstrated that small networks (826 total parameters) achieve
    accurate optical component characterisation. This implementation uses an
    equivalent architecture for Q-factor drift prediction, with online training
    on accumulated session data — no pre-training or offline dataset needed.

    Normalisation: StandardScaler (zero mean, unit variance) applied to
    input windows before training and prediction. This is the correct
    normalisation strategy for MLP networks.

    Fallback: linear regression (parent class) is used when:
      — scikit-learn is not installed
      — ANN is disabled in config
      — Fewer than min_training_samples Q values are available

    Publication citation:
      "Inspired by Anderson (2023)'s demonstration of small-scale ANN
       feasibility for optical component characterisation, a lightweight
       feedforward neural network (2 hidden layers, 8 neurons each) is
       employed for Q-factor drift prediction, replacing linear regression
       with a nonlinear estimator capable of detecting complex drift patterns."
    """

    def __init__(self, cfg: SystemConfig, logger: SystemLogger):
        super().__init__(cfg, logger)
        sec = "ann_drift_predictor"
        self._enabled     = bool(cfg.get(sec, "enabled"))
        self._window_size = int(cfg.get(sec, "window_size"))
        self._min_samples = int(cfg.get(sec, "min_training_samples"))
        hl                = cfg.get(sec, "hidden_layer_sizes")
        self._hidden      = tuple(int(h) for h in hl)
        self._max_iter    = int(cfg.get(sec, "max_iter"))
        self._alpha_reg   = float(cfg.get(sec, "alpha_regularization"))

        # Prediction log for dashboard panel — (q_history_index, actual_q, predicted_q)
        self._prediction_log: List[Tuple[int, float, float]] = []

        self._log.debug(
            f"ANNDriftPredictor: window={self._window_size}  "
            f"hidden={self._hidden}  min_samples={self._min_samples}  "
            f"sklearn={SKLEARN_AVAILABLE}"
        )

    def analyse(self, q_history: List[float]) -> Dict:
        """
        Predict next Q using ANN if conditions are met, else linear regression.
        Returns same dict structure as DriftPredictor.analyse() with ann_used flag.
        """
        can_use_ann = (
            SKLEARN_AVAILABLE
            and self._enabled
            and len(q_history) >= self._min_samples + self._window_size
        )
        if not can_use_ann:
            reason = (
                "sklearn unavailable"     if not SKLEARN_AVAILABLE else
                "ANN disabled in config"  if not self._enabled else
                f"only {len(q_history)} samples "
                f"(need {self._min_samples + self._window_size})"
            )
            self._log.debug(f"[ANN] Fallback to linear regression: {reason}")
            return super().analyse(q_history)
        return self._ann_analyse(q_history)

    def _ann_analyse(self, q_history: List[float]) -> Dict:
        """Build sliding-window training set, train MLP, predict next Q."""
        q = np.array(q_history, dtype=float)
        w = self._window_size

        # Build sliding-window training set
        X, y = [], []
        for i in range(len(q) - w):
            X.append(q[i: i + w])
            y.append(q[i + w])
        X = np.array(X);  y = np.array(y)

        # Normalise with StandardScaler (zero mean, unit variance)
        scaler = StandardScaler()
        X_sc   = scaler.fit_transform(X)

        # Train MLP — Anderson-inspired 2×8 architecture
        model = MLPRegressor(
            hidden_layer_sizes=self._hidden,
            activation="relu",
            max_iter=self._max_iter,
            alpha=self._alpha_reg,
            random_state=42,
            warm_start=False
        )
        model.fit(X_sc, y)

        # Predict from most recent window
        last_window    = q[-w:].reshape(1, -1)
        last_sc        = scaler.transform(last_window)
        predicted_q    = float(model.predict(last_sc)[0])

        # Linear regression for slope/R² statistics (supplement to ANN)
        lin_result  = super().analyse(q_history)
        slope       = lin_result["slope"]
        r_squared   = lin_result["r_squared"]

        # Drift detection: ANN predicts close to threshold OR linear trend negative
        q_min           = self._cfg.get("supervisory", "q_target_minimum")
        slope_threshold = self._cfg.get("supervisory",
                                        "drift_detection_slope_threshold")
        drift_detected  = (predicted_q < q_min + 0.3
                           or slope < slope_threshold)

        # Training accuracy on most recent held-out point
        train_pred  = float(model.predict(X_sc[-1:, :])[0])
        pred_error  = abs(train_pred - float(y[-1]))

        # Record for dashboard
        self._prediction_log.append((len(q_history), float(q[-1]), predicted_q))

        if drift_detected:
            self._log.warning(
                f"[ANN] DRIFT DETECTED: pred_Q={predicted_q:.4f}  "
                f"LinSlope={slope:.4f}  train_error={pred_error:.4f}"
            )
        else:
            self._log.debug(
                f"[ANN] pred_Q={predicted_q:.4f}  "
                f"current_Q={q[-1]:.4f}  train_error={pred_error:.4f}"
            )
        return {
            "slope":          slope,
            "r_squared":      r_squared,
            "drift_detected": drift_detected,
            "predicted_next": predicted_q,
            "ann_used":       True,
            "train_error":    pred_error
        }

    @property
    def prediction_log(self) -> List[Tuple[int, float, float]]:
        """(index, actual_q, predicted_q) tuples for dashboard plotting."""
        return self._prediction_log


# ══════════════════════════════════════════════════════════════════════════════
# 9.  EDFA OPTIMIZER  (Adaptive Momentum-Gradient)
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class EDFAState:
    current_gain: float
    best_gain:    float
    best_q:       float
    step_size:    float
    direction:    float
    momentum:     float = 0.0
    iteration:    int   = 0
    gain_history: List[float] = field(default_factory=list)
    q_history:    List[float] = field(default_factory=list)
    converged:    bool  = False
    oscillating:  bool  = False


class EDFAOptimizer:
    """
    Adaptive Momentum-Gradient EDFA Gain Optimizer.

    Algorithm:
      1. Momentum update: p = β·p + (1−β)·direction·step
      2. Candidate gain  = current + p
      3. If EWMA Q improves: accept candidate, grow step (+10%)
         If EWMA Q worsens:  reverse direction, shrink step (−40%), reset momentum
      4. Oscillation detection: if ≥3 direction reversals in last 4 steps → halve step
      5. Convergence: 2 consecutive iterations with ΔQ < tolerance
    """

    def __init__(self, cfg: SystemConfig, logger: SystemLogger,
                 optisys: OptiSystemInterface, monitor: SignalMonitor):
        self._cfg     = cfg;  self._log = logger
        self._optisys = optisys;  self._monitor = monitor
        self._gain_min  = cfg.get("edfa_optimizer", "gain_min_db")
        self._gain_max  = cfg.get("edfa_optimizer", "gain_max_db")
        self._step_min  = cfg.get("edfa_optimizer", "step_min_db")
        self._step_max  = cfg.get("edfa_optimizer", "step_max_db")
        self._beta      = cfg.get("edfa_optimizer", "momentum_beta")
        self._tolerance = cfg.get("edfa_optimizer", "convergence_tolerance")
        self._max_iter  = cfg.get("edfa_optimizer", "max_iterations")
        self._osc_win   = cfg.get("edfa_optimizer", "oscillation_window")

    def initialize(self) -> EDFAState:
        init_gain = self._cfg.get("edfa_optimizer", "initial_gain_db")
        init_step = self._cfg.get("edfa_optimizer", "initial_step_db")
        self._log.section("EDFA OPTIMIZER — INITIALIZATION")
        self._optisys.set_edfa_gain(init_gain)
        _, ewma_q = self._monitor.measure()
        state = EDFAState(current_gain=init_gain, best_gain=init_gain,
                          best_q=ewma_q, step_size=init_step, direction=1.0)
        state.gain_history.append(init_gain)
        state.q_history.append(ewma_q)
        self._log.info(f"Baseline → Gain={init_gain:.2f} dB  Q(EWMA)={ewma_q:.4f}")
        return state

    def optimize(self) -> EDFAState:
        state  = self.initialize()
        stable = 0
        for i in range(self._max_iter):
            state.iteration = i + 1
            # Oscillation detection
            if len(state.q_history) >= self._osc_win:
                if self._count_oscillations(state.q_history) >= self._osc_win - 1:
                    state.step_size  = max(state.step_size * 0.4, self._step_min)
                    state.oscillating = True
                    self._log.warning(
                        f"[EDFA {i+1}] Oscillation → step={state.step_size:.4f} dB"
                    )
            # Momentum candidate
            gradient_step  = state.direction * state.step_size
            state.momentum = (self._beta * state.momentum
                              + (1.0 - self._beta) * gradient_step)
            candidate = float(np.clip(state.current_gain + state.momentum,
                                      self._gain_min, self._gain_max))
            self._optisys.set_edfa_gain(candidate)
            raw_q, ewma_q = self._monitor.measure()
            prev_best = state.best_q

            if ewma_q > state.best_q:
                state.current_gain = candidate;  state.best_q = ewma_q
                state.best_gain    = candidate
                state.step_size    = min(state.step_size * 1.1, self._step_max)
            else:
                state.direction *= -1.0
                state.step_size  = max(state.step_size * 0.6, self._step_min)
                state.momentum   = 0.0

            state.gain_history.append(candidate)
            state.q_history.append(ewma_q)
            self._log.info(
                f"[EDFA {i+1:02d}] Gain={candidate:.3f} dB | "
                f"Q_raw={raw_q:.4f} | Q_EWMA={ewma_q:.4f} | "
                f"step={state.step_size:.4f} | dir={state.direction:+.0f} | "
                f"best_Q={state.best_q:.4f}"
            )
            if abs(ewma_q - prev_best) < self._tolerance:
                stable += 1
                if stable >= 2:
                    state.converged = True
                    self._log.info(
                        f"EDFA Converged iter={i+1} | "
                        f"Gain={state.best_gain:.3f} dB | Q={state.best_q:.4f}"
                    )
                    break
            else:
                stable = 0

        self._optisys.set_edfa_gain(state.best_gain)
        self._log.info(
            f"EDFA done: Gain={state.best_gain:.3f} dB | "
            f"Q={state.best_q:.4f} | Converged={state.converged}"
        )
        return state

    @staticmethod
    def _count_oscillations(q_hist: List[float]) -> int:
        diffs = np.diff(q_hist[-4:])
        return sum(1 for j in range(len(diffs) - 1)
                   if diffs[j] * diffs[j + 1] < 0)


# ══════════════════════════════════════════════════════════════════════════════
# 10. MZM BIAS CONTROLLER  (Momentum-Gradient + Operating Point Awareness)
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class MZMState:
    bias_correction: float
    best_correction: float
    best_q:          float
    step_size:       float
    direction:       float
    momentum:        float = 0.0
    iteration:       int   = 0
    bias_history:            List[float] = field(default_factory=list)
    q_history:               List[float] = field(default_factory=list)
    stabilized:              bool        = False
    op_point_history:        List[str]   = field(default_factory=list)
    harmonic_ratio_history:  List[float] = field(default_factory=list)
    bias_normalized_history: List[float] = field(default_factory=list)


class MZMBiasController:
    """
    Adaptive Momentum-Gradient MZM Bias Recovery Controller.

    Core algorithm (same as EDFA optimizer — applied to bias voltage):
      Momentum-gradient with adaptive step, convergence detection.

    v3.0 Anderson enhancements:
      — MZMTransferModel operating point classification at each iteration
      — Region-aware step multiplier from MZMTransferModel.step_multiplier()
      — Harmonic ratio logged at each step (Bessel function model)
      — Vπ-normalised correction in all log messages and reports
    """

    def __init__(self, cfg: SystemConfig, logger: SystemLogger,
                 optisys: OptiSystemInterface, monitor: SignalMonitor,
                 tfm: MZMTransferModel):
        self._cfg     = cfg;  self._log = logger
        self._optisys = optisys;  self._monitor = monitor
        self._tfm     = tfm

        self._drift_v   = cfg.get("mzm_controller", "drift_injection_voltage")
        self._step_min  = cfg.get("mzm_controller", "step_min")
        self._step_max  = cfg.get("mzm_controller", "step_max")
        self._tolerance = cfg.get("mzm_controller", "tolerance")
        self._max_iter  = cfg.get("mzm_controller", "max_iterations")
        self._beta      = cfg.get("mzm_controller", "momentum_beta")
        self._bias_min  = cfg.get("mzm_controller", "bias_min_v")
        self._bias_max  = cfg.get("mzm_controller", "bias_max_v")

    def inject_drift_and_measure(self) -> Tuple[float, float]:
        """Inject thermal drift and characterise the degraded operating point."""
        self._log.section("MZM BIAS CONTROLLER — DRIFT INJECTION")
        self._log.info(
            f"Injecting thermal drift: {self._drift_v:.3f} V  "
            f"({self._tfm.normalize_bias(self._drift_v):.3f} × Vπ)"
        )
        self._optisys.inject_thermal_drift(self._drift_v)
        _, q_drifted = self._monitor.measure()
        op     = self._tfm.classify_operating_point(self._drift_v)
        hratio = self._tfm.harmonic_ratio(self._drift_v)
        vn     = self._tfm.normalize_bias(self._drift_v)
        self._log.info(
            f"Post-drift Q={q_drifted:.4f} | "
            f"OP={op.value} | HarmonicRatio={hratio:.3f} | V/Vπ={vn:.3f}"
        )
        return self._drift_v, q_drifted

    def recover(self) -> MZMState:
        """Run adaptive momentum-gradient bias recovery with operating-point awareness."""
        drift_v, q_drifted = self.inject_drift_and_measure()
        init_step = self._cfg.get("mzm_controller", "initial_step_size")
        state = MZMState(bias_correction=0.0, best_correction=0.0,
                         best_q=q_drifted, step_size=init_step, direction=1.0)
        self._log.section("MZM BIAS CONTROLLER — ADAPTIVE RECOVERY")
        stable = 0

        for i in range(self._max_iter):
            state.iteration = i + 1
            effective_bias  = float(np.clip(drift_v + state.bias_correction,
                                            self._bias_min, self._bias_max))
            self._optisys.set_mzm_bias(effective_bias)
            raw_q, ewma_q = self._monitor.measure()

            # Operating point + harmonic ratio at current bias
            op      = self._tfm.classify_operating_point(effective_bias)
            h_ratio = self._tfm.harmonic_ratio(effective_bias)
            v_norm  = self._tfm.normalize_bias(effective_bias)
            state.bias_history.append(effective_bias)
            state.q_history.append(ewma_q)
            state.op_point_history.append(op.value)
            state.harmonic_ratio_history.append(h_ratio)
            state.bias_normalized_history.append(v_norm)
            prev_best = state.best_q

            # Region-aware step multiplier
            step_mult     = self._tfm.step_multiplier(op)
            eff_step      = state.step_size * step_mult
            gradient_step = state.direction * eff_step
            state.momentum = (self._beta * state.momentum
                              + (1.0 - self._beta) * gradient_step)
            candidate_correction = state.bias_correction + state.momentum

            test_bias = float(np.clip(drift_v + candidate_correction,
                                      self._bias_min, self._bias_max))
            self._optisys.set_mzm_bias(test_bias)
            _, q_test = self._monitor.measure()

            if q_test > ewma_q:
                state.bias_correction = candidate_correction
                state.best_q          = q_test
                state.best_correction = candidate_correction
                state.step_size = min(state.step_size * 1.08, self._step_max)
            else:
                state.direction *= -1.0
                state.step_size  = max(state.step_size * 0.55, self._step_min)
                state.momentum   = 0.0

            self._log.info(
                f"[MZM {i+1:02d}] "
                f"Bias={effective_bias:.4f} V ({v_norm:.3f}Vπ) | "
                f"Q_raw={raw_q:.4f} | Q_EWMA={ewma_q:.4f} | "
                f"corr={state.bias_correction:.4f} V | "
                f"step={state.step_size:.4f} | "
                f"OP={op.value} | HR={h_ratio:.2f} | best_Q={state.best_q:.4f}"
            )

            if abs(ewma_q - prev_best) < self._tolerance:
                stable += 1
                if stable >= 2:
                    state.stabilized = True
                    self._log.info(
                        f"MZM Stabilized iter={i+1} | "
                        f"corr={state.best_correction:.4f} V "
                        f"({self._tfm.normalize_bias(state.best_correction):.4f} × Vπ) | "
                        f"Q={state.best_q:.4f}"
                    )
                    break
            else:
                stable = 0

        final_bias = float(np.clip(drift_v + state.best_correction,
                                   self._bias_min, self._bias_max))
        self._optisys.set_mzm_bias(final_bias)
        self._log.info(
            f"MZM done: corr={state.best_correction:.4f} V | "
            f"({self._tfm.normalize_bias(state.best_correction):.4f} × Vπ) | "
            f"Q={state.best_q:.4f} | Stabilized={state.stabilized}"
        )
        return state


# ══════════════════════════════════════════════════════════════════════════════
# 11. SUPERVISORY INTELLIGENCE LAYER
# ══════════════════════════════════════════════════════════════════════════════

class SystemState(Enum):
    INITIALIZING  = auto()
    OPTIMIZING    = auto()
    STABLE        = auto()
    DEGRADING     = auto()
    CRITICAL      = auto()
    RECALIBRATING = auto()


@dataclass
class SupervisoryReport:
    # Core fields
    session_id:              str
    start_time:              float
    end_time:                float
    initial_q:               float
    final_q:                 float
    edfa_optimal_gain:       float
    mzm_optimal_correction:  float
    convergence_time_s:      float
    stability_index:         float
    drift_events:            int
    recalibration_count:     int
    optimization_efficiency: float
    final_state:             str
    # Anderson (2023) fields
    operation_mode:          str   = "CALIBRATION"
    operating_point_final:   str   = "UNKNOWN"
    harmonic_ratio_final:    float = 0.0
    bias_normalized_vpi:     float = 0.0
    ann_predictor_used:      bool  = False
    predicted_q_accuracy:    float = 0.0


class SupervisoryIntelligence:
    """
    Top-level orchestrator. Runs phases based on OperationMode.

    CALIBRATION → Phase 1 (EDFA) + Phase 2 (MZM) + Phase 3 (drift check)
    OPERATIONAL → Phase 2 (MZM only) + Phase 3 (drift check)
    MONITORING  → 5 Q readings + Phase 3 (drift prediction, no actuation)
    """

    def __init__(self, cfg: SystemConfig, logger: SystemLogger,
                 optisys: OptiSystemInterface, monitor: SignalMonitor,
                 drift_predictor: ANNDriftPredictor,
                 edfa_optimizer: EDFAOptimizer,
                 mzm_controller: MZMBiasController,
                 tfm: MZMTransferModel):
        self._cfg     = cfg;  self._log = logger
        self._optisys = optisys;  self._monitor = monitor
        self._drift   = drift_predictor
        self._edfa    = edfa_optimizer
        self._mzm     = mzm_controller
        self._tfm     = tfm

        self._q_min    = cfg.get("supervisory", "q_target_minimum")
        self._q_excel  = cfg.get("supervisory", "q_excellent_threshold")
        self._instab   = cfg.get("supervisory", "instability_std_threshold")
        self._op_mode  = cfg.get_operation_mode()

        self._current_state      = SystemState.INITIALIZING
        self._q_history: List[float] = []
        self._recalib_count      = 0
        self._drift_events       = 0
        self._session_start      = time.time()
        self._initial_q          = 0.0
        self._edfa_state: Optional[EDFAState] = None
        self._mzm_state:  Optional[MZMState]  = None

        self._log.info(
            f"SupervisoryIntelligence: mode={self._op_mode.value}  "
            f"ANN={'ON' if SKLEARN_AVAILABLE else 'OFF (sklearn missing)'}"
        )

    def _classify_state(self, q: float) -> SystemState:
        std = self._monitor.recent_std()
        if q >= self._q_excel and std < self._instab:
            return SystemState.STABLE
        elif q < self._q_min:
            return SystemState.CRITICAL
        return SystemState.DEGRADING

    def run_full_optimization_session(self) -> SupervisoryReport:
        self._session_start = time.time()
        session_id = str(uuid.uuid4())[:8]
        self._log.section(
            f"SUPERVISORY SESSION START  [{session_id}]  "
            f"Mode={self._op_mode.value}"
        )

        # Phase 1: EDFA (CALIBRATION only)
        if self._op_mode == OperationMode.CALIBRATION:
            self._current_state = SystemState.OPTIMIZING
            self._log.info("Phase 1: EDFA Gain Optimization")
            self._edfa_state = self._edfa.optimize()
            self._initial_q  = self._edfa_state.q_history[0]
            self._q_history.extend(self._edfa_state.q_history)
        else:
            self._log.info(f"Phase 1: Skipped (Mode={self._op_mode.value})")

        # Phase 2: MZM (CALIBRATION + OPERATIONAL)
        if self._op_mode in (OperationMode.CALIBRATION, OperationMode.OPERATIONAL):
            self._log.info("Phase 2: MZM Thermal Drift Recovery")
            self._mzm_state = self._mzm.recover()
            if self._initial_q == 0.0:
                self._initial_q = (self._mzm_state.q_history[0]
                                   if self._mzm_state.q_history else 0.0)
            self._q_history.extend(self._mzm_state.q_history)
        else:
            self._log.info("Phase 2: Skipped (MONITORING mode)")

        # MONITORING: collect Q readings only
        if self._op_mode == OperationMode.MONITORING:
            self._log.section("MONITORING MODE — READ-ONLY")
            for i in range(5):
                _, q_val = self._monitor.measure()
                self._q_history.append(q_val)
                self._log.info(f"[MON {i+1:02d}] Q_EWMA={q_val:.4f}")
            self._initial_q = self._q_history[0] if self._q_history else 0.0

        # Phase 3: Supervisory drift check
        self._log.info("Phase 3: Post-Optimization Supervisory Drift Check")
        current_q    = self._q_history[-1] if self._q_history else 0.0
        drift_result = self._drift.analyse(self._q_history)

        if drift_result["drift_detected"] and self._op_mode != OperationMode.MONITORING:
            self._drift_events += 1
            self._log.warning(
                f"Drift confirmed via {'ANN' if drift_result['ann_used'] else 'LinReg'}. "
                f"Predicted next Q={drift_result['predicted_next']:.4f}. "
                f"Triggering proactive recalibration..."
            )
            self._current_state = SystemState.RECALIBRATING
            self._mzm_state     = self._mzm.recover()
            self._recalib_count += 1
            current_q           = self._mzm_state.best_q
            self._q_history.extend(self._mzm_state.q_history)

        self._current_state = self._classify_state(current_q)
        self._log.info(
            f"Final state: {self._current_state.name} | Q={current_q:.4f}"
        )

        # Final operating point + harmonic ratio at recovered bias
        final_correction = self._mzm_state.best_correction if self._mzm_state else 0.0
        final_bias_v     = (self._cfg.get("mzm_controller",
                                          "drift_injection_voltage") + final_correction)
        op_final  = self._tfm.classify_operating_point(final_bias_v)
        hr_final  = self._tfm.harmonic_ratio(final_bias_v)
        vn_final  = self._tfm.normalize_bias(final_correction)

        pred_accuracy = 0.0
        if drift_result.get("ann_used") and len(self._q_history) >= 2:
            pred_accuracy = abs(drift_result["predicted_next"] - current_q)

        end_time   = time.time()
        stab_idx   = self._compute_stability_index()
        opt_eff    = current_q / max(self._initial_q, 0.001)
        edfa_gain  = (self._edfa_state.best_gain if self._edfa_state
                      else self._cfg.get("edfa_optimizer", "initial_gain_db"))

        report = SupervisoryReport(
            session_id=session_id, start_time=self._session_start,
            end_time=end_time, initial_q=self._initial_q, final_q=current_q,
            edfa_optimal_gain=edfa_gain,
            mzm_optimal_correction=final_correction,
            convergence_time_s=(end_time - self._session_start),
            stability_index=stab_idx, drift_events=self._drift_events,
            recalibration_count=self._recalib_count,
            optimization_efficiency=opt_eff,
            final_state=self._current_state.name,
            operation_mode=self._op_mode.value,
            operating_point_final=op_final.value,
            harmonic_ratio_final=hr_final,
            bias_normalized_vpi=vn_final,
            ann_predictor_used=drift_result.get("ann_used", False),
            predicted_q_accuracy=pred_accuracy
        )
        self._print_report(report)
        return report

    def _print_report(self, report: SupervisoryReport) -> None:
        self._log.section("SESSION COMPLETE — PERFORMANCE REPORT")
        self._log.info(f"  Session ID              : {report.session_id}")
        self._log.info(f"  Operation Mode          : {report.operation_mode}")
        self._log.info(f"  Initial Q               : {report.initial_q:.4f}")
        self._log.info(f"  Final Q                 : {report.final_q:.4f}")
        self._log.info(f"  EDFA Optimal Gain       : {report.edfa_optimal_gain:.3f} dB")
        self._log.info(f"  MZM Bias Correction     : {report.mzm_optimal_correction:.4f} V")
        self._log.info(f"  MZM Correction (Vπ)     : {report.bias_normalized_vpi:.4f} × Vπ")
        self._log.info(f"  Final Operating Point   : {report.operating_point_final}")
        self._log.info(f"  Final Harmonic Ratio    : {report.harmonic_ratio_final:.3f}")
        self._log.info(f"  ANN Predictor Used      : {report.ann_predictor_used}")
        self._log.info(f"  ANN Prediction Accuracy : ±{report.predicted_q_accuracy:.4f}")
        self._log.info(f"  Convergence Time        : {report.convergence_time_s:.2f} s")
        self._log.info(f"  Stability Index         : {report.stability_index:.4f}")
        self._log.info(f"  Drift Events            : {report.drift_events}")
        self._log.info(f"  Recalibrations          : {report.recalibration_count}")
        self._log.info(f"  Optimization Efficiency : {report.optimization_efficiency:.3f}×")
        self._log.info(f"  Final State             : {report.final_state}")

    def _compute_stability_index(self) -> float:
        vals = np.array(self._q_history[-10:])
        if len(vals) < 2 or np.mean(vals) == 0:
            return 0.0
        return float(max(0.0, 1.0 - (np.std(vals) / np.mean(vals))))


# ══════════════════════════════════════════════════════════════════════════════
# 12. PERFORMANCE DASHBOARD  (9-Panel Publication Figure)
# ══════════════════════════════════════════════════════════════════════════════

_OP_COLORS = {
    "NEAR_PEAK":       "#2ecc71",   # green  — optimal / close to optimal
    "NEAR_QUADRATURE": "#f39c12",   # amber  — linear region (acceptable)
    "NEAR_NULL":       "#e74c3c",   # red    — minimum transmission (bad)
    "UNKNOWN":         "#95a5a6"    # grey   — transition / unclassified
}


class PerformanceDashboard:
    """
    9-panel (3×3) publication-quality performance dashboard.

    Panel layout:
    ┌──────────────────────┬──────────────────────┬──────────────────────┐
    │ [0,0] EDFA:          │ [0,1] EDFA:          │ [0,2] MZM:           │
    │  Gain vs Q Factor    │  Q Convergence+SG    │  Bias Trajectory     │
    ├──────────────────────┼──────────────────────┼──────────────────────┤
    │ [1,0] MZM:           │ [1,1] Full Q Drift   │ [1,2] ANN Predicted  │
    │  Q Recovery          │  Analysis + Trend    │  vs Actual Q         │
    ├──────────────────────┼──────────────────────┼──────────────────────┤
    │ [2,0] Harmonic Ratio │ [2,1] Operating Point│ [2,2] Performance    │
    │  vs Bias + Theory    │  Timeline (bar chart)│  Metrics Table       │
    └──────────────────────┴──────────────────────┴──────────────────────┘
    """

    def __init__(self, logger: SystemLogger, tfm: MZMTransferModel):
        self._log = logger
        self._tfm = tfm

    def plot(self, edfa_state: Optional[EDFAState],
             mzm_state: Optional[MZMState],
             report: SupervisoryReport,
             q_combined: List[float],
             drift_predictor: ANNDriftPredictor) -> None:

        fig = plt.figure(figsize=(18, 12))
        fig.suptitle(
            "Intelligent FPGA Supervisory Control — Performance Dashboard\n"
            "[Anderson 2023 Research Enhancements Applied]\n"
            f"Session: {report.session_id}  |  Mode: {report.operation_mode}  |  "
            f"Final Q: {report.final_q:.3f}  |  "
            f"EDFA Gain: {report.edfa_optimal_gain:.2f} dB  |  "
            f"Stability Index: {report.stability_index:.4f}",
            fontsize=10, fontweight="bold", y=0.99
        )
        gs = gridspec.GridSpec(3, 3, figure=fig, hspace=0.55, wspace=0.40)

        # ── [0,0] EDFA Gain vs Q ─────────────────────────────────────────────
        ax1 = fig.add_subplot(gs[0, 0])
        if edfa_state and len(edfa_state.gain_history) > 0:
            ax1.plot(edfa_state.gain_history, edfa_state.q_history,
                     marker='o', color='royalblue', lw=1.5, ms=4)
            ax1.axvline(edfa_state.best_gain, color='red', ls='--', lw=1.2,
                        label=f"Opt={edfa_state.best_gain:.2f} dB")
            ax1.legend(fontsize=8)
        else:
            ax1.text(0.5, 0.5, "EDFA skipped\n(OPERATIONAL/MONITORING mode)",
                     ha='center', va='center', transform=ax1.transAxes,
                     fontsize=9, color='grey')
        ax1.set_xlabel("EDFA Gain (dB)", fontsize=9)
        ax1.set_ylabel("Q Factor (EWMA)", fontsize=9)
        ax1.set_title("EDFA: Gain vs Q Factor", fontsize=10, fontweight='bold')
        ax1.grid(True, alpha=0.4)

        # ── [0,1] EDFA Q Convergence + Savitzky-Golay ───────────────────────
        ax2 = fig.add_subplot(gs[0, 1])
        if edfa_state and len(edfa_state.q_history) >= 2:
            iters_e = list(range(1, len(edfa_state.q_history) + 1))
            ax2.plot(iters_e, edfa_state.q_history, color='royalblue',
                     lw=1.2, marker='s', ms=3, alpha=0.7, label='Q (EWMA)')
            ax2.axhline(edfa_state.best_q, color='green', ls='--', lw=1.2,
                        label=f"Best={edfa_state.best_q:.3f}")
            if len(edfa_state.q_history) >= 5:
                sg = min(len(edfa_state.q_history), 5)
                sg = sg if sg % 2 == 1 else sg - 1
                ax2.plot(iters_e, savgol_filter(edfa_state.q_history, sg, 2),
                         color='orange', lw=1.5, ls='--', alpha=0.9,
                         label='SG Trend')
            ax2.legend(fontsize=8)
        else:
            ax2.text(0.5, 0.5, "No EDFA data", ha='center', va='center',
                     transform=ax2.transAxes, fontsize=9, color='grey')
        ax2.set_xlabel("Iteration", fontsize=9)
        ax2.set_ylabel("Q Factor (EWMA)", fontsize=9)
        ax2.set_title("EDFA: Q Convergence", fontsize=10, fontweight='bold')
        ax2.grid(True, alpha=0.4)

        # ── [0,2] MZM Bias Trajectory ────────────────────────────────────────
        ax3 = fig.add_subplot(gs[0, 2])
        if mzm_state and len(mzm_state.bias_history) > 0:
            ax3.plot(mzm_state.bias_history, mzm_state.q_history,
                     marker='D', color='darkorange', lw=1.5, ms=4)
            ax3.axhline(mzm_state.best_q, color='green', ls='--', lw=1.2,
                        label=f"Best Q={mzm_state.best_q:.3f}")
            ax3.legend(fontsize=8)
        else:
            ax3.text(0.5, 0.5, "No MZM data", ha='center', va='center',
                     transform=ax3.transAxes, fontsize=9, color='grey')
        ax3.set_xlabel("MZM Bias Voltage (V)", fontsize=9)
        ax3.set_ylabel("Q Factor (EWMA)", fontsize=9)
        ax3.set_title("MZM: Bias Recovery Trajectory", fontsize=10, fontweight='bold')
        ax3.grid(True, alpha=0.4)

        # ── [1,0] MZM Q Recovery ─────────────────────────────────────────────
        ax4 = fig.add_subplot(gs[1, 0])
        if mzm_state and len(mzm_state.q_history) > 0:
            iters_m = list(range(1, len(mzm_state.q_history) + 1))
            ax4.plot(iters_m, mzm_state.q_history, color='darkorange',
                     lw=1.5, marker='o', ms=3)
            ax4.axhline(mzm_state.best_q, color='green', ls='--', lw=1.2,
                        label=f"Recovered={mzm_state.best_q:.3f}")
            ax4.legend(fontsize=8)
        else:
            ax4.text(0.5, 0.5, "No MZM data", ha='center', va='center',
                     transform=ax4.transAxes, fontsize=9, color='grey')
        ax4.set_xlabel("Iteration", fontsize=9)
        ax4.set_ylabel("Q Factor (EWMA)", fontsize=9)
        ax4.set_title("MZM: Q Recovery (EWMA)", fontsize=10, fontweight='bold')
        ax4.grid(True, alpha=0.4)

        # ── [1,1] Full Q Drift Analysis ──────────────────────────────────────
        ax5 = fig.add_subplot(gs[1, 1])
        if len(q_combined) >= 2:
            x_all = np.arange(len(q_combined), dtype=float)
            ax5.plot(x_all, q_combined, color='steelblue', lw=1.2,
                     alpha=0.8, label='Q History (EWMA)')
            if len(q_combined) >= 4:
                sl, inter, r_val, *_ = stats.linregress(x_all, q_combined)
                ax5.plot(x_all, sl * x_all + inter, 'r--', lw=1.5,
                         label=f"Trend={sl:.3f}/step  R²={r_val**2:.2f}")
            ax5.legend(fontsize=8)
        ax5.set_xlabel("Total Iteration Index", fontsize=9)
        ax5.set_ylabel("Q Factor (EWMA)", fontsize=9)
        ax5.set_title("Drift Analysis: Q Trend", fontsize=10, fontweight='bold')
        ax5.grid(True, alpha=0.4)

        # ── [1,2] ANN Predicted vs Actual Q  [Anderson 2023] ─────────────────
        ax6 = fig.add_subplot(gs[1, 2])
        pred_log = drift_predictor.prediction_log
        if len(pred_log) > 0:
            idx_v    = [p[0] for p in pred_log]
            actual_v = [p[1] for p in pred_log]
            pred_v   = [p[2] for p in pred_log]
            ax6.plot(idx_v, actual_v, 'bo-', ms=5, lw=1.2, label='Actual Q')
            ax6.plot(idx_v, pred_v,   'r^--', ms=5, lw=1.2, label='ANN Predicted Q')
            avg_err = np.mean([abs(a - p) for a, p in zip(actual_v, pred_v)])
            ax6.set_title(
                f"ANN Drift Predictor  [Anderson 2023]\n"
                f"Avg Prediction Error: {avg_err:.4f}",
                fontsize=9, fontweight='bold'
            )
            ax6.legend(fontsize=8)
        else:
            ax6.text(
                0.5, 0.5,
                "ANN fallback active\n"
                f"(need ≥{drift_predictor._min_samples + drift_predictor._window_size}"
                " Q samples)\nUsing linear regression",
                ha='center', va='center', transform=ax6.transAxes,
                fontsize=9, color='navy'
            )
            ax6.set_title("ANN Drift Predictor  [Anderson 2023]",
                          fontsize=9, fontweight='bold')
        ax6.set_xlabel("Q History Index", fontsize=9)
        ax6.set_ylabel("Q Factor", fontsize=9)
        ax6.grid(True, alpha=0.4)

        # ── [2,0] Harmonic Ratio vs Bias + Theoretical Curve ─────────────────
        ax7 = fig.add_subplot(gs[2, 0])
        if mzm_state and len(mzm_state.bias_history) > 0:
            ax7.plot(mzm_state.bias_history,
                     mzm_state.harmonic_ratio_history,
                     'g^-', ms=5, lw=1.5, label='Measured HR')
            ax7.axhline(1.0, color='grey', ls=':', lw=1.0, label='HR=1 reference')
            # Theoretical curve overlay
            v_range  = np.linspace(
                min(mzm_state.bias_history) - 0.2,
                max(mzm_state.bias_history) + 0.2, 300)
            hr_curve = [self._tfm.harmonic_ratio(v) for v in v_range]
            ax7t = ax7.twinx()
            ax7t.plot(v_range, hr_curve, 'b--', lw=1.0, alpha=0.4,
                      label='Theoretical HR')
            ax7t.set_ylabel("Theoretical HR", fontsize=8, color='blue')
            ax7.legend(fontsize=7, loc='upper left')
        else:
            ax7.text(0.5, 0.5, "No harmonic data", ha='center', va='center',
                     transform=ax7.transAxes, fontsize=9, color='grey')
        ax7.set_xlabel("MZM Bias Voltage (V)", fontsize=9)
        ax7.set_ylabel("Harmonic Ratio", fontsize=9)
        ax7.set_title(
            "Analytical Harmonic Ratio vs Bias\n[Anderson 2023, Eq 4.4/4.5]",
            fontsize=9, fontweight='bold'
        )
        ax7.grid(True, alpha=0.4)

        # ── [2,1] Operating Point Timeline ───────────────────────────────────
        ax8 = fig.add_subplot(gs[2, 1])
        if mzm_state and len(mzm_state.op_point_history) > 0:
            iters_op = list(range(1, len(mzm_state.op_point_history) + 1))
            colors   = [_OP_COLORS.get(op, "#95a5a6")
                        for op in mzm_state.op_point_history]
            ax8.bar(iters_op, [1.0] * len(iters_op), color=colors,
                    width=0.7, alpha=0.9)
            # Annotate each bar with harmonic ratio value
            if len(mzm_state.harmonic_ratio_history) == len(iters_op):
                for x, hr in zip(iters_op, mzm_state.harmonic_ratio_history):
                    ax8.text(x, 0.5, f"{hr:.1f}", ha='center', va='center',
                             fontsize=7, color='white', fontweight='bold')
            patches = [mpatches.Patch(color=c, label=k.replace("_", " "))
                       for k, c in _OP_COLORS.items()]
            ax8.legend(handles=patches, fontsize=7, loc='upper right')
        else:
            ax8.text(0.5, 0.5, "No operating point data",
                     ha='center', va='center', transform=ax8.transAxes,
                     fontsize=9, color='grey')
        ax8.set_yticks([])
        ax8.set_xlabel("MZM Iteration", fontsize=9)
        ax8.set_title(
            "Operating Point Timeline\n[Anderson 2023, Sec 4.2]\n"
            "(numbers = harmonic ratio)",
            fontsize=9, fontweight='bold'
        )
        ax8.grid(True, alpha=0.2, axis='x')

        # ── [2,2] Extended Performance Metrics Table ──────────────────────────
        ax9 = fig.add_subplot(gs[2, 2])
        ax9.axis('off')
        metrics = [
            ("Session ID",           report.session_id),
            ("Operation Mode",       report.operation_mode),
            ("Initial Q",            f"{report.initial_q:.4f}"),
            ("Final Q",              f"{report.final_q:.4f}"),
            ("Q Improvement",        f"+{report.final_q - report.initial_q:.4f}"),
            ("EDFA Optimal Gain",    f"{report.edfa_optimal_gain:.3f} dB"),
            ("MZM Correction",       f"{report.mzm_optimal_correction:.4f} V"),
            ("Correction (Vπ)",      f"{report.bias_normalized_vpi:.4f} × Vπ"),
            ("Vπ",                   f"{self._tfm.vpi:.1f} V"),
            ("Final Operating Point", report.operating_point_final),
            ("Final Harmonic Ratio", f"{report.harmonic_ratio_final:.3f}"),
            ("ANN Used",             str(report.ann_predictor_used)),
            ("ANN Accuracy",         f"±{report.predicted_q_accuracy:.4f}"),
            ("Convergence Time",     f"{report.convergence_time_s:.2f} s"),
            ("Stability Index",      f"{report.stability_index:.4f}"),
            ("Drift Events",         str(report.drift_events)),
            ("Recalibrations",       str(report.recalibration_count)),
            ("Opt. Efficiency",      f"{report.optimization_efficiency:.3f}×"),
            ("Final State",          report.final_state),
        ]
        for idx, (label, value) in enumerate(metrics):
            y_pos = 0.97 - idx * 0.051
            color = 'darkgreen' if idx >= 7 else 'navy'
            ax9.text(0.02, y_pos, label + ":", fontsize=7.5,
                     fontweight='bold', transform=ax9.transAxes, va='top')
            ax9.text(0.60, y_pos, value, fontsize=7.5,
                     transform=ax9.transAxes, va='top', color=color)
        ax9.set_title("Performance Metrics\n(green = Anderson 2023 fields)",
                      fontsize=9, fontweight='bold')
        ax9.set_xlim(0, 1);  ax9.set_ylim(0, 1)

        plt.savefig("fpga_control_dashboard_FINAL.png", dpi=150,
                    bbox_inches='tight')
        self._log.info("Dashboard saved → fpga_control_dashboard_FINAL.png")
        plt.show()


# ══════════════════════════════════════════════════════════════════════════════
# 13. MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    """
    Assemble all modules and run a complete supervisory session.

    Dependency chain:
      cfg → logger → optisys → tfm → monitor
      → drift_pred → edfa_opt → mzm_ctrl → supervisor → dashboard
    """
    mode = SimulationMode.REAL if WIN32_AVAILABLE else SimulationMode.VIRTUAL

    cfg    = SystemConfig()
    logger = SystemLogger(cfg)

    logger.section("INTELLIGENT FPGA SUPERVISORY CONTROL SYSTEM — FINAL")
    logger.section("Anderson (2023) Research Enhancements Active")
    logger.info(f"Run Mode          : {mode.name}")
    logger.info(f"Operation Mode    : {cfg.get_operation_mode().value}")
    logger.info(f"EWMA alpha        : {cfg.get('mzm_transfer_model', 'ewma_alpha')}")
    logger.info(f"Vπ               : {cfg.get('mzm_transfer_model', 'vpi_voltage')} V")
    logger.info(f"ANN Predictor     : {'ON' if SKLEARN_AVAILABLE else 'OFF — install scikit-learn'}")

    optisys    = OptiSystemInterface(cfg, logger, mode)
    tfm        = MZMTransferModel(cfg, logger)
    monitor    = SignalMonitor(cfg, logger, optisys)
    drift_pred = ANNDriftPredictor(cfg, logger)
    edfa_opt   = EDFAOptimizer(cfg, logger, optisys, monitor)
    mzm_ctrl   = MZMBiasController(cfg, logger, optisys, monitor, tfm)
    supervisor = SupervisoryIntelligence(
        cfg, logger, optisys, monitor,
        drift_pred, edfa_opt, mzm_ctrl, tfm
    )
    dashboard  = PerformanceDashboard(logger, tfm)

    report = supervisor.run_full_optimization_session()

    combined_q: List[float] = []
    if supervisor._edfa_state:
        combined_q.extend(supervisor._edfa_state.q_history)
    if supervisor._mzm_state:
        combined_q.extend(supervisor._mzm_state.q_history)
    if not combined_q:
        combined_q = list(supervisor._q_history)

    dashboard.plot(
        supervisor._edfa_state,
        supervisor._mzm_state,
        report,
        combined_q,
        drift_pred
    )

    logger.info(
        "Session complete. "
        "Dashboard → fpga_control_dashboard_FINAL.png  |  "
        "Logs → logs/"
    )
    return report


if __name__ == "__main__":
    main()
