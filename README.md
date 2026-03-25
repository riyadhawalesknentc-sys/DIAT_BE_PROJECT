# 🔬 Intelligent FPGA-Inspired Supervisory Control System for a 100 Gbps WDM Optical Communication Network



<p align="center">
  <img src="https://img.shields.io/badge/Domain-Optical%20Communications-blue?style=for-the-badge"/>
  <img src="https://img.shields.io/badge/Simulation-OptiSystem%20v23-orange?style=for-the-badge"/>
  <img src="https://img.shields.io/badge/Control-FPGA--Inspired%20Python-green?style=for-the-badge"/>
  <img src="https://img.shields.io/badge/Speed-100%20Gbps-red?style=for-the-badge"/>
  <img src="https://img.shields.io/badge/Status-Final%20Submission-brightgreen?style=for-the-badge"/>
</p>

---

## 📌 Project Overview

This project presents an **Intelligent FPGA-Inspired Supervisory Control System** designed to manage and optimize a **100 Gbps Wavelength Division Multiplexed (WDM) Optical Communication Network** simulated in **OptiSystem v23**.

The system implements adaptive, real-time control over two critical optical components:
- **EDFA (Erbium-Doped Fiber Amplifier)** — Gain optimization across 4 fiber spans
- **MZM (Mach-Zehnder Modulator)** — Thermal drift detection and automatic bias recovery

Control intelligence is delivered via a **Python supervisory layer** that mimics FPGA reprogrammable hardware logic, incorporating research enhancements from **Anderson (2023)** including EWMA smoothing, Bessel-function harmonic analysis, ANN-based drift prediction, and a 3-mode operational architecture.

---

## 🏫 Academic Details

| Field | Detail |
|---|---|
| **Institution** | Savitribai Phule Pune University — SKNCOE, Pune |
| **Degree** | Bachelor of Engineering — Electronics & Telecommunication |
| **Project Phase** | Phase 2 — Final Submission |
| **Academic Year** | 2024–2025 |
| **Reference Paper** | Anderson, M. J. (2023) — *FPGA-Based Control for Optical Communication Systems* |

---

## 🌐 System Specifications

| Parameter | Value |
|---|---|
| **Total Bit Rate** | 100 Gbps |
| **Architecture** | 4 × 25 Gbps NRZ channels |
| **Channel Frequencies** | 193.1 THz — 193.4 THz |
| **Channel Spacing** | 100 GHz |
| **Fiber Type** | SMF + DCF (Dispersion Compensating Fiber) |
| **Total Link Distance** | 387 km |
| **Number of EDFA Spans** | 4 |
| **Modulation Format** | NRZ (Non-Return-to-Zero) |
| **Simulation Tool** | Optiwave OptiSystem v23 |
| **Python–OptiSystem Interface** | Windows COM (win32com) |

---

## 🏗️ System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│              INTELLIGENT SUPERVISORY CONTROL SYSTEM             │
│                   (FPGA-Inspired Python Layer)                   │
│                                                                 │
│  ┌──────────────┐   ┌──────────────┐   ┌────────────────────┐  │
│  │ EDFA         │   │ MZM Bias     │   │ ANN Drift          │  │
│  │ Gain         │   │ Controller   │   │ Predictor          │  │
│  │ Optimizer    │   │ (Momentum +  │   │ (MLPRegressor      │  │
│  │ (Adaptive    │   │  Harmonic    │   │  sklearn)          │  │
│  │  Momentum)   │   │  Ratio)      │   │                    │  │
│  └──────┬───────┘   └──────┬───────┘   └────────┬───────────┘  │
│         │                  │                     │              │
│  ┌──────▼──────────────────▼─────────────────────▼───────────┐  │
│  │               SUPERVISORY STATE MACHINE                    │  │
│  │   (CALIBRATION / OPERATIONAL / MONITORING modes)           │  │
│  └────────────────────────────┬────────────────────────────┘  │
│                               │ COM API (win32com)             │
└───────────────────────────────┼─────────────────────────────────┘
                                │
                    ┌───────────▼──────────────┐
                    │   OptiSystem v23          │
                    │   (Optical Network Sim)   │
                    │   BER Analyzer → Q-Factor │
                    └──────────────────────────┘
```

---

## ⚙️ Key Features & Research Enhancements

All enhancements are based on **Anderson (2023)** and correctly implemented in the control script:

### [A] EWMA Smoothing *(Anderson 2023, Section 4.4)*
> Q_ewma = α · Q_new + (1−α) · Q_prev &nbsp;&nbsp; [α = 0.3]

Exponentially Weighted Moving Average applied after IQR outlier rejection, ensuring noise-robust Q-factor readings before any control decision.

### [B] MZM Transfer Model *(Anderson 2023, Equation 4.1)*
> T(V) = ½ [1 + cos(π · V / Vπ)] &nbsp;&nbsp; [Vπ = 4.0 V]

A dedicated `MZMTransferModel` class encapsulates the full electro-optic transfer curve, enabling precise bias point calculations.

### [C] Analytical Harmonic Ratio *(Anderson 2023, Equations 4.4 & 4.5)*
> Fund ∝ |sin(πV/Vπ)| × J₁(πVac/Vπ) &nbsp;&nbsp; [Bessel J₁]  
> Harm2 ∝ |cos(πV/Vπ)| × J₂(πVac/Vπ) &nbsp;&nbsp; [Bessel J₂]

Simulation-domain equivalent of Anderson's Goertzel FPGA filter, used to detect MZM operating point without additional hardware.

### [D] Operating Point Classifier *(Anderson 2023, Section 4.2)*
Four classes: `NEAR_PEAK` | `NEAR_QUADRATURE` | `NEAR_NULL` | `UNKNOWN`  
Region-aware step multipliers ensure faster convergence far from quadrature and fine tuning near it.

### [E] Vπ-Normalised Bias Reporting *(Anderson 2023, Section 6.3)*
All corrections expressed as V/Vπ — modulator-independent fractional units for universal reporting.

### [F] ANN Drift Predictor *(Anderson 2023, Section 6.3)*
```
Architecture:  5-sample Q window → Dense(8, ReLU) → Dense(8, ReLU) → Q̂_next
Library:       sklearn MLPRegressor + StandardScaler
Fallback:      Linear Regression when < 10 samples available
```
Online training on accumulated Q-factor history. Prediction log stored for dashboard plotting.

### [G] Three-Mode Operation Architecture *(Anderson 2023, Chapter 3)*

| Mode | Description | Use Case |
|---|---|---|
| `CALIBRATION` | Full EDFA sweep + MZM recovery + drift check | System startup / first run |
| `OPERATIONAL` | MZM correction only, EDFA skipped | Routine in-service use |
| `MONITORING` | Read-only Q logging + ANN prediction | Passive health monitoring |

### [H] 9-Panel Publication Dashboard
Auto-generated 3×3 matplotlib figure includes:
- Q-factor timeline (raw + EWMA + ANN prediction)
- EDFA gain optimization trajectory
- MZM bias correction curve
- Harmonic ratio vs. bias with theoretical overlay
- Operating point bar-chart timeline
- And more — saved as PNG after each session

---

## 📁 Repository Structure

```
📦 intelligent-fpga-wdm-control/
 ┣ 📄 intelligent_fpga_control_FINAL.py   ← Main Python control script
 ┣ 📄 config.json                          ← All tunable system parameters
 ┣ 📄 README.md                            ← This file
 ┣ 🖼️  100Gbps_WDM_Photonics_System_Architecture_with_FPGA_Control.png
 ┣ 📂 logs/                                ← Auto-generated session logs
 ┃  ┗ 📄 fpga_control_session.log
 ┣ 📂 docs/
 ┃  ┣ 📄 FINAL_BE_Project_Report_Phase2_SUBMISSION.docx
 ┃  ┗ 📄 Chapter10_Results_Performance_Evaluation.docx
```

---

## 🛠️ Dependencies & Installation

### Prerequisites
- **Windows OS** (required for OptiSystem COM interface)
- **Optiwave OptiSystem v23** (simulation engine)
- **Python 3.9+**

### Python Libraries

```bash
pip install numpy matplotlib scipy scikit-learn pywin32
```

| Library | Purpose |
|---|---|
| `numpy` | Numerical computation |
| `matplotlib` | 9-panel dashboard plotting |
| `scipy` | Bessel functions, statistical filters |
| `scikit-learn` | ANN drift predictor (MLPRegressor) |
| `pywin32` | OptiSystem COM API bridge (win32com) |

---

## 🚀 How to Run

### Step 1 — Configure Parameters
Edit `config.json` to set your desired operation mode and component names:

```json
{
  "operation_mode": "CALIBRATION",
  "optisystem": {
    "ber_analyzer_name": "BER Analyzer_3",
    "mzm_name": "MZM",
    "edfa_names": ["EDFA", "EDFA_1", "EDFA_2", "EDFA_3"]
  }
}
```

### Step 2 — Open OptiSystem
Launch OptiSystem v23 and open your `.osd` project file. Make sure the simulation is **not running**.

### Step 3 — Run the Control Script
```bash
python intelligent_fpga_control_FINAL.py
```

The script will:
1. Connect to OptiSystem via COM
2. Read Q-factor from `BER Analyzer_3`
3. Optimize EDFA gains across all 4 spans
4. Inject simulated thermal drift (+0.6 V) into MZM bias
5. Recover optimal MZM bias automatically
6. Train ANN predictor and forecast future drift
7. Save 9-panel dashboard + session log

---

## 📊 Key Results

| Metric | Value |
|---|---|
| **Initial Q-Factor** | ~5.8 dB |
| **Optimized Q-Factor** | ~7.4 – 7.5 dB |
| **Q Target (Minimum)** | 6.0 dB |
| **Q Excellent Threshold** | 7.2 dB |
| **MZM Drift Injected** | +0.6 V |
| **Drift Recovery** | ✅ Full recovery within 20 iterations |
| **EDFA Convergence** | ✅ Within 30 iterations |
| **BER** | < 10⁻⁹ (post-optimization) |

---

## 🧠 Control Algorithm Flowchart

```
START
  │
  ├─ Load config.json
  ├─ Connect to OptiSystem COM
  ├─ Read Q-factor (EWMA + IQR filtered)
  │
  ├─ [CALIBRATION MODE]
  │     ├─ EDFA Gain Sweep (momentum-gradient, spans 0–3)
  │     ├─ Inject MZM Drift (+0.6 V)
  │     ├─ Harmonic Ratio → Classify Operating Point
  │     ├─ MZM Bias Recovery (momentum-gradient)
  │     └─ ANN Drift Prediction
  │
  ├─ [OPERATIONAL MODE]
  │     └─ MZM correction only
  │
  ├─ [MONITORING MODE]
  │     └─ Log Q + Predict drift, no actuation
  │
  ├─ Generate 9-Panel Dashboard
  └─ Save Logs → END
```

---

## 📚 References

1. **Anderson, M. J. (2023).** *FPGA-Based Supervisory Control for Adaptive Optical Communication Systems.* [Primary research reference — all algorithmic enhancements sourced from this work]
2. Agrawal, G. P. — *Fiber-Optic Communication Systems*, 5th Edition
3. Optiwave Corporation — *OptiSystem Component Library & Tutorials*, v23
4. Saleh, B. E. A. & Teich, M. C. — *Fundamentals of Photonics*, 3rd Edition

---

## 👨‍💻 Authors

**Final Year BE Project Team**  
Department of Electronics & Telecommunication Engineering  
Sinhgad College of Engineering (SKNCOE), Pune  
Savitribai Phule Pune University  
Academic Year: 2024–2025

---

## 📜 License

This project is submitted as an academic final year project at SKNCOE, Pune. All rights reserved. The code and documentation are intended for educational and research purposes only.

---

<p align="center">
  <i>Built with 💡 optics, ⚡ FPGA logic, and ☕ a lot of debugging</i>
</p>
