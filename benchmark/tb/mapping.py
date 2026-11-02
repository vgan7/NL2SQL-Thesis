import re

SRC_TABLE = "factory_telemetry_fixed"
VIEW = "hist_1m"

COLS = {
    "id": "rid",
    "ts": "t_stamp",
    "machine_id": "eq_no",
    "machine_type": "eq_cls",
    "machine_name": "eq_tag",
    "temperature": "tt_101_pv",
    "vibration": "vt_310_pv",
    "humidity": "mt_120_pv",
    "pressure": "pt_204_pv",
    "energy_consumption": "jq_501_pv",
    "anomaly_flag": "xs_401_st",
    "maintenance_required": "xs_402_st",
    "downtime_risk": "xs_403_pv",
    "predicted_remaining_life": "ky_510_pv",
    "failure_type": "alm_code",
}

VALUES = {
    "machine_type": {
        "Hydraulic Press": "PRS",
        "CNC Lathe": "LTH",
        "Robotic Arm": "RBT",
        "Cooling System": "CLG",
        "Packaging Unit": "PKG",
    },
    "failure_type": {
        "Normal": "F00",
        "Electrical Fault": "F01",
        "Overheating": "F02",
        "Pressure Drop": "F03",
        "Vibration Issue": "F04",
    },
}

ENCODE = {orig: code for m in VALUES.values() for orig, code in m.items()}
DECODE = {code: orig for orig, code in ENCODE.items()}

def _ident_re(names):
    return re.compile(r"\b(" + "|".join(sorted(names, key=len, reverse=True)) + r")\b")


_IDENT_SQL = _ident_re([SRC_TABLE, *COLS])
_IDENT_PROSE = _ident_re([SRC_TABLE, "ts", *(c for c in COLS if "_" in c)])
_LITERAL_RE = re.compile("'(" + "|".join(re.escape(v) for v in ENCODE) + ")'")


def to_opaque(text, prose=False):
    if not text:
        return text
    text = _LITERAL_RE.sub(lambda m: f"'{ENCODE[m.group(1)]}'", text)
    ident = _IDENT_PROSE if prose else _IDENT_SQL
    return ident.sub(lambda m: VIEW if m.group(1) == SRC_TABLE else COLS[m.group(1)], text)


def view_ddl(source="raw.factory_telemetry_fixed"):
    cols = []
    for orig, new in COLS.items():
        if orig in VALUES:
            cases = " ".join(f"WHEN '{o}' THEN '{c}'" for o, c in VALUES[orig].items())
            cols.append(f"  (CASE {orig} {cases} END)::varchar AS {new}")
        else:
            cols.append(f"  {orig} AS {new}")
    return f"CREATE OR REPLACE VIEW public.{VIEW} AS\nSELECT\n" + ",\n".join(cols) + f"\nFROM {source};\n"
