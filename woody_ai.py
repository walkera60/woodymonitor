from __future__ import annotations
import json, math, os, re, smtplib, sqlite3, ssl, threading, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from statistics import mean, median
from collections import defaultdict
import time
from zoneinfo import ZoneInfo
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
SETTINGS = DATA / "ai_settings.json"
REPORT = DATA / "ai_latest_report.txt"
DB = DATA / "woody.db"
TZ = ZoneInfo("Europe/Copenhagen")
router = APIRouter(prefix="/api/v1/ai", tags=["AI"])
LOCK = threading.RLock()
JOB = {"state": "idle", "message": "", "started_at": None, "finished_at": None}
DEFAULTS = {
    "enabled": False, "gemini_api_key": "", "gemini_model": "gemini-3.8-flash",
    "analysis_days": 7, "target_indoor_temp": 21.0,
    "email_enabled": False, "email_to": "", "smtp_host": "",
    "smtp_port": 465, "smtp_security": "ssl", "smtp_username": "",
    "smtp_password": "", "smtp_from": "", "last_analysis": None,
    "last_email": None, "last_error": None, "max_input_tokens": 24000
}
PARAMS = ["indoor_temp", "outside_temp", "boiler_temp", "boiler_return_temp",
          "hotwater_temp", "smoke_temp", "oxygen", "power", "power_kW", "feeder_time"]
RANGES = {"indoor_temp": (5, 40), "outside_temp": (-40, 50),
          "boiler_temp": (0, 120), "boiler_return_temp": (0, 120),
          "hotwater_temp": (0, 100), "smoke_temp": (-20, 400),
          "oxygen": (0, 25), "power": (0, 100), "power_kW": (0, 30)}
SECRET_FIELDS = ("gemini_api_key", "smtp_password")

class SettingsUpdate(BaseModel):
    enabled: bool | None = None
    gemini_api_key: str | None = None
    gemini_model: str | None = None
    analysis_days: int | None = Field(None, ge=1, le=30)
    target_indoor_temp: float | None = Field(None, ge=10, le=30)
    email_enabled: bool | None = None
    email_to: str | None = None
    smtp_host: str | None = None
    smtp_port: int | None = Field(None, ge=1, le=65535)
    smtp_security: str | None = None
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    max_input_tokens: int | None = Field(None, ge=1000, le=100000)

def load():
    with LOCK:
        d = dict(DEFAULTS)
        if SETTINGS.exists():
            d.update(json.loads(SETTINGS.read_text()))
        return d

def save(d):
    with LOCK:
        DATA.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS.with_suffix(".tmp")
        with open(tmp, "w") as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump(d, f, indent=2, ensure_ascii=False)
        os.replace(tmp, SETTINGS)
        os.chmod(SETTINGS, 0o600)

def public(d):
    d = dict(d)
    for key in SECRET_FIELDS:
        d[key + "_configured"] = bool(d.get(key))
        d[key] = ""
    return d

def stamp():
    return datetime.now(timezone.utc).isoformat()

def parse_time(s):
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)

def fetch(days):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = {p: [] for p in PARAMS}
    rejected = {p: 0 for p in PARAMS}
    if not DB.exists():
        raise RuntimeError("Historikdatabasen findes ikke.")
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=20) as db:
        db.row_factory = sqlite3.Row
        q = ",".join("?" for _ in PARAMS)
        rows = db.execute(f"SELECT timestamp,parameter,value FROM measurements WHERE parameter IN ({q}) AND timestamp >= ? ORDER BY timestamp", [*PARAMS, cutoff.strftime("%Y-%m-%dT%H:%M:%S")])
        for r in rows:
            p = r["parameter"]
            try:
                t = parse_time(r["timestamp"])
                v = float(r["value"])
                if t < cutoff or t > datetime.now(timezone.utc) + timedelta(minutes=5):
                    continue
                if not math.isfinite(v) or (p in RANGES and not RANGES[p][0] <= v <= RANGES[p][1]):
                    rejected[p] += 1
                    continue
                out[p].append((t, v))
            except (ValueError, TypeError, OverflowError):
                rejected[p] += 1
    return out, rejected

def stats(rows):
    if not rows:
        return {"samples": 0}
    v = [x[1] for x in rows]
    return {"samples": len(v), "min": round(min(v), 3), "max": round(max(v), 3),
            "avg": round(mean(v), 3), "first": round(v[0], 3), "last": round(v[-1], 3)}

def buckets(rows, minutes=15):
    result = {}
    for t, v in rows:
        local = t.astimezone(TZ)
        key = local.replace(minute=(local.minute // minutes)*minutes, second=0, microsecond=0)
        result.setdefault(key, []).append(v)
    return {k: mean(v) for k, v in result.items()}

def _calibration():
    # Læs Woody Monitors eksisterende feeder-kalibrering direkte.
    # Importér IKKE woody_monitor her, da hovedprogrammet allerede kører
    # som __main__ og ellers risikerer at blive initialiseret en ekstra gang.
    path = DATA / "feeder_settings.json"

    grams = 1200.0
    seconds = 360.0

    if path.exists():
        try:
            c = json.loads(path.read_text())
            grams = float(c.get("grams", grams))
            seconds = float(c.get("seconds", seconds))
        except Exception as exc:
            raise ValueError(
                f"Kunne ikke læse feeder-kalibrering: {exc}"
            ) from exc

    if not (0 < grams <= 10000 and 0 < seconds <= 3600):
        raise ValueError("Ugyldig feeder-kalibrering")

    return {
        "grams": grams,
        "seconds": seconds,
        "grams_per_second": grams / seconds,
    }

def _cycles(rows, threshold=1.0, gap_minutes=10):
    # Require an observed inactive sample to separate cycles; missing data is not OFF.
    cycles, current = [], []
    for t,v in rows:
        if v > threshold:
            if current and (t-current[-1][0]).total_seconds() > gap_minutes*60:
                cycles.append(current); current=[]
            current.append((t,v))
        elif current:
            cycles.append(current); current=[]
    if current: cycles.append(current)
    return cycles

def _near(rows, t, max_minutes=10):
    if not rows:
        return None

    # rows er allerede sorteret efter timestamp.
    # Brug binær søgning direkte på rækken i stedet for at opbygge
    # en ny liste med alle timestamps ved hvert opslag.
    import bisect

    i = bisect.bisect_left(
        rows,
        t,
        key=lambda row: row[0]
    )

    candidates = []

    if i > 0:
        candidates.append(rows[i - 1])

    if i < len(rows):
        candidates.append(rows[i])

    if not candidates:
        return None

    best = min(
        candidates,
        key=lambda row: abs((row[0] - t).total_seconds())
    )

    if abs((best[0] - t).total_seconds()) > max_minutes * 60:
        return None

    return best[1]

def _cycle_details(data):
    result=[]
    for cycle in _cycles(data["power"]):
        start,end=cycle[0][0],cycle[-1][0]
        if (end-start).total_seconds()<60:continue
        row={"start":start.astimezone(TZ).isoformat(),"last_active":end.astimezone(TZ).isoformat(),
             "observed_duration_minutes":round((end-start).total_seconds()/60,1),
             "duration_note":"Fra første til sidste aktive prøve; ikke præcis brændertid.",
             "max_power_percent":max(v for _,v in cycle),"mean_power_percent":round(mean(v for _,v in cycle),2)}
        for p in ("indoor_temp","outside_temp","boiler_temp","boiler_return_temp","hotwater_temp","smoke_temp"):
            row[p]={label:_near(data[p],t) for label,t in (("before",start-timedelta(minutes=15)),("start",start),("end",end),("after_1h",end+timedelta(hours=1)),("after_2h",end+timedelta(hours=2)))}
        result.append(row)
    return result

def _cooling(data):
    # Find højst ét 2-timers observationsvindue pr. lokal nat.
    # Der analyseres kun faktiske målinger, og manglende data interpoleres ikke.
    import bisect

    indoor = data["indoor_temp"]
    power = data["power"]
    outside = data["outside_temp"]

    if not indoor:
        return []

    result = []
    analysed_nights = set()

    for t, indoor_start in indoor:
        local_t = t.astimezone(TZ)

        if local_t.hour not in (22, 23, 0, 1, 2, 3, 4, 5, 6):
            continue

        # 00:00-06:59 hører til natten, der begyndte dagen før.
        night_date = (
            (local_t - timedelta(days=1)).date()
            if local_t.hour < 7
            else local_t.date()
        )

        if night_date in analysed_nights:
            continue

        end = t + timedelta(hours=2)
        indoor_end = _near(indoor, end, 10)

        if indoor_end is None:
            continue

        p0 = bisect.bisect_left(
            power,
            t,
            key=lambda row: row[0]
        )
        p1 = bisect.bisect_right(
            power,
            end,
            key=lambda row: row[0]
        )
        observed_power = power[p0:p1]

        if len(observed_power) < 2:
            continue

        # Afkølingsmålingen må kun bruges, når fyret var observeret inaktivt.
        if any(value > 1 for _, value in observed_power):
            continue

        # Store huller gør perioden uegnet.
        if any(
            (b[0] - a[0]).total_seconds() > 600
            for a, b in zip(observed_power, observed_power[1:])
        ):
            continue

        o0 = bisect.bisect_left(
            outside,
            t,
            key=lambda row: row[0]
        )
        o1 = bisect.bisect_right(
            outside,
            end,
            key=lambda row: row[0]
        )
        outside_values = [
            value
            for _, value in outside[o0:o1]
        ]

        result.append({
            "night": night_date.isoformat(),
            "start": local_t.isoformat(),
            "indoor_start": round(indoor_start, 3),
            "indoor_end": round(indoor_end, 3),
            "change_c_per_hour": round(
                (indoor_end - indoor_start) / 2,
                3
            ),
            "outside_mean": (
                round(mean(outside_values), 2)
                if outside_values
                else None
            ),
            "note": (
                "Observeret temperaturændring under registreret "
                "inaktiv brænder; ikke i sig selv bevis for bygningens "
                "varmetabskoefficient."
            )
        })

        analysed_nights.add(night_date)

    return result

def payload(days, target):
    data,rejected=fetch(days)
    if not data["indoor_temp"]:raise RuntimeError("Ingen gyldige indendørstemperaturer. Kontroller HA-sensoren.")
    b={p:buckets(rows) for p,rows in data.items() if p!="feeder_time"}
    times=sorted(set().union(*(set(x) for x in b.values())))
    timeline=[]
    for t in times:
        row={"time":t.isoformat()}
        row.update({p:round(values[t],3) for p,values in b.items() if t in values})
        timeline.append(row)
    feeder=data["feeder_time"]
    delta=0; excluded=0
    for (ta,a),(tb,v) in zip(feeder,feeder[1:]):
        d=v-a; elapsed=(tb-ta).total_seconds()
        if 0<=d<=elapsed+2 and 0<elapsed<=3600:delta+=d
        elif d!=0:excluded+=1
    calibration=_calibration()
    oxygen=data["oxygen"]
    active=[v for t,v in oxygen if (_near(data["power"],t,2) or 0)>1]
    quality=[]
    if active and max(active)-min(active)<0.2:
        quality.append("O2 er næsten konstant under aktiv fyrdrift; kan ikke bruges til forbrændingsdiagnose uden sensorverifikation.")
    daily=defaultdict(list)
    for t,v in b["indoor_temp"].items():daily[t.date().isoformat()].append(v)
    return {"period_days":days,"target_indoor_temp_c":target,"timezone":"Europe/Copenhagen",
      "statistics":{p:stats(rows) for p,rows in data.items()},"rejected_samples":rejected,
      "indoor_daily":{d:{"min":round(min(v),2),"max":round(max(v),2)} for d,v in daily.items()},
      "feeder_runtime_delta_seconds":round(delta,2),"feeder_excluded_intervals":excluded,
      "feeder_calibration":calibration,"feeder_consumption_kg":round(delta*calibration["grams_per_second"]/1000,3),
      "consumption_note":"Estimat fra gyldige positive tællerdeltaer og aktuel kalibrering. Ekskluderede intervaller kan give underestimat.",
      "burner_cycles":_cycle_details(data),"night_cooling_observations":_cooling(data),
      "data_quality":quality,"timeline_15min":timeline,
      "limitations":["15-minutters middelværdier er ikke samtidige råmålinger.",
      "Manglende intervaller må ikke interpoleres til sikre observationer.",
      "Kedelopvarmning beviser ikke rumopvarmning; varmtvandskilde er ikke identificeret.",
      "En uge i september dokumenterer ikke vinterens varmebehov.",
      "Ingen automatisk styring eller ændring af sikkerhedsindstillinger."]}

def prompt(d):
    return """Du er en forsigtig dansk energianalytiker for et Woody/Scotte pillefyr.
Skriv en dansk rapport med konklusion, datagrundlag, indetemperatur, varmebehov,
fyringscyklusser, natlig afkøling, varmtvand, kalibreret pilleforbrug, afvigelser
og konkrete forslag til yderligere målinger. Skeln mellem observation, hypotese
og anbefaling. Angiv kun start/stop-klokkeslæt hvis flere sammenlignelige forløb
understøtter dem; ellers sig at datagrundlaget er utilstrækkeligt. En målt
indetemperatur over målet beviser ikke at varmen alene er passiv. Varmtvandskilden
er ukendt; påstå ikke at den er elpatron, lagret varme eller fyr uden evidens.
Konstant O2 under drift er ikke en valid forbrændingsmåling. Brug ikke denne til
justering af luft, ilt eller sikkerhedsparametre. Skeln mellem målt og estimeret
pilleforbrug, og angiv usikkerhed og ekskluderede tællerdeltaer. Ingen automatisk
styring, ingen ændring af sikkerhedsgrænser, ingen vilkårlig afbrydelse af fyr.
Brug lokale tider Europe/Copenhagen. Rapporten er rådgivning, ikke udført ændring.
DATA:
"""+json.dumps(d,ensure_ascii=False,separators=(",",":"))

def gemini(d, settings):
    import time

    primary_model = settings["gemini_model"]

    if not re.fullmatch(r"[A-Za-z0-9._-]+", primary_model):
        raise RuntimeError("Ugyldigt Gemini-modelnavn.")

    # Den valgte model bruges altid først.
    # Derefter stabile fallback-modeller.
    fallback_models = [
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash-lite",
    ]

    models = [primary_model]

    for model in fallback_models:
        if model not in models:
            models.append(model)

    # Begræns request-størrelse.
    # Statistik og cyklusser bevares.
    d = dict(d)

    limit = int(
        settings.get("max_input_tokens", 24000)
    ) * 3

    while (
        len(prompt(d)) > limit
        and len(d.get("timeline_15min", [])) > 24
    ):
        d["timeline_15min"] = (
            d["timeline_15min"][::2]
        )

        d["timeline_sampling_note"] = (
            "Tidslinjen er reduceret for at begrænse "
            "API-forbrug; statistik og cyklusser er bevaret."
        )

    prompt_text = prompt(d)

    if len(prompt_text) > limit:
        raise RuntimeError(
            "Datamængden er for stor. "
            "Vælg en kortere analyseperiode."
        )

    body = {
        "contents": [
            {
                "parts": [
                    {
                        "text": prompt_text
                    }
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 6000,
        },
    }

    encoded_body = json.dumps(body).encode()

    # Kun disse betragtes som server-/kapacitetsfejl
    # hvor model-fallback giver mening.
    fallback_http_codes = {
        500,
        502,
        503,
        504,
    }

    total_models = len(models)
    errors = []

    for model_index, model in enumerate(
        models,
        start=1
    ):
        is_fallback = model != primary_model

        if is_fallback:
            with LOCK:
                JOB["message"] = (
                    f"Gemini skifter til fallback-model "
                    f"{model} "
                    f"({model_index}/{total_models})…"
                )

        url = (
            "https://generativelanguage.googleapis.com/"
            f"v1beta/models/{model}:generateContent"
        )

        # To forsøg pr. model.
        for attempt in range(1, 3):

            if attempt > 1:
                with LOCK:
                    JOB["message"] = (
                        f"Gemini {model} er midlertidigt "
                        f"optaget – nyt forsøg {attempt}/2 "
                        f"om 2 sek…"
                    )

                time.sleep(2)

            else:
                with LOCK:
                    if is_fallback:
                        JOB["message"] = (
                            f"Gemini analyserer med "
                            f"fallback-model {model}…"
                        )
                    else:
                        JOB["message"] = (
                            f"Gemini analyserer med "
                            f"{model}…"
                        )

            req = urllib.request.Request(
                url,
                data=encoded_body,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key":
                        settings["gemini_api_key"],
                },
                method="POST",
            )

            try:
                with urllib.request.urlopen(
                    req,
                    timeout=120
                ) as response:
                    result = json.load(response)

                text = "\n".join(
                    part.get("text", "")
                    for candidate
                    in result.get("candidates", [])
                    for part
                    in candidate.get(
                        "content", {}
                    ).get("parts", [])
                    if part.get("text")
                ).strip()

                if not text:
                    raise RuntimeError(
                        "Gemini returnerede ingen rapport."
                    )

                # Returnér både rapport og faktisk model.
                return (
                    text,
                    model,
                    is_fallback,
                )

            except urllib.error.HTTPError as exc:
                code = exc.code

                errors.append(
                    f"{model}: HTTP {code}"
                )

                # 429 er kvote/rate-limit.
                # Vi skjuler den ikke med model-fallback.
                if code == 429:
                    raise RuntimeError(
                        "Gemini HTTP 429. "
                        "API-kvoten er nået eller "
                        "forespørgslerne rate-begrænses."
                    ) from None

                # Permanente fejl.
                if code not in fallback_http_codes:

                    messages = {
                        400:
                            "Ugyldig forespørgsel "
                            "til Gemini.",
                        401:
                            "Gemini API-nøglen "
                            "blev ikke accepteret.",
                        403:
                            "Gemini-adgang blev afvist.",
                        404:
                            "Modellen er ikke "
                            "tilgængelig for projektet.",
                    }

                    raise RuntimeError(
                        f"Gemini HTTP {code}. "
                        + messages.get(
                            code,
                            "Kontroller model, "
                            "API-nøgle og konfiguration."
                        )
                    ) from None

                # Første serverfejl:
                # prøv samme model én gang til.
                if attempt == 1:
                    continue

                # Andet forsøg fejlede.
                # Fortsæt til næste model.
                break

            except urllib.error.URLError:
                if attempt == 1:
                    with LOCK:
                        JOB["message"] = (
                            "Forbindelsen til Gemini "
                            "fejlede – prøver igen om "
                            "2 sek…"
                        )

                    time.sleep(2)
                    continue

                raise RuntimeError(
                    "Forbindelsen til Gemini fejlede. "
                    "Kontroller internetforbindelsen."
                ) from None

            except TimeoutError:
                if attempt == 1:
                    continue

                raise RuntimeError(
                    "Gemini svarede ikke inden for "
                    "tidsgrænsen."
                ) from None

            except RuntimeError:
                raise

            except Exception:
                if attempt == 1:
                    continue

                raise RuntimeError(
                    "Forbindelsen til Gemini fejlede. "
                    "Kontroller netværk og konfiguration."
                ) from None

    details = ", ".join(errors)

    raise RuntimeError(
        "Alle Gemini-modeller var midlertidigt "
        "utilgængelige. "
        + (
            f"Forsøg: {details}"
            if details
            else ""
        )
    )


def _mail_value(value, decimals=1, suffix=""):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "–"

    return (
        f"{value:.{decimals}f}"
        .replace(".", ",")
        + suffix
    )


def _mail_series(data, parameter):
    result = []

    if not data:
        return result

    for row in data.get("timeline_15min", []):
        if parameter not in row:
            continue

        try:
            t = datetime.fromisoformat(row["time"])
            v = float(row[parameter])
            result.append((t, v))
        except (KeyError, TypeError, ValueError):
            pass

    return result


def _mail_average(data, parameter):
    values = [
        v for _, v in _mail_series(data, parameter)
    ]

    if not values:
        return None

    return sum(values) / len(values)


def _mail_period(data):
    if not data:
        return "Seneste analyse"

    rows = data.get("timeline_15min", [])

    if not rows:
        return "Seneste analyse"

    try:
        first = datetime.fromisoformat(
            rows[0]["time"]
        ).astimezone(TZ)

        last = datetime.fromisoformat(
            rows[-1]["time"]
        ).astimezone(TZ)

        return (
            f"{first.strftime('%d.%m.%Y')} – "
            f"{last.strftime('%d.%m.%Y')}"
        )

    except Exception:
        return "Seneste analyse"


def _mail_inline(text):
    import html
    import re

    text = html.escape(str(text))

    text = re.sub(
        r"\*\*(.+?)\*\*",
        r"<strong>\1</strong>",
        text,
    )

    text = re.sub(
        r"`([^`]+)`",
        (
            r'<code style="background:#eef1f5;'
            r'padding:2px 5px;border-radius:4px;">'
            r'\1</code>'
        ),
        text,
    )

    return text


def _mail_report_html(report):
    import re

    output = []
    list_open = None

    def close_list():
        nonlocal list_open

        if list_open:
            output.append(f"</{list_open}>")
            list_open = None

    for raw in str(report).splitlines():
        line = raw.strip()

        if not line:
            close_list()
            continue

        if line == "---":
            close_list()
            output.append(
                '<hr style="border:0;'
                'border-top:1px solid #e5e7eb;'
                'margin:26px 0;">'
            )
            continue

        if line.startswith("# "):
            continue

        if line.startswith("## "):
            close_list()
            output.append(
                '<h2 style="font-size:19px;'
                'color:#182334;margin:28px 0 12px;">'
                + _mail_inline(line[3:])
                + "</h2>"
            )
            continue

        if line.startswith("### "):
            close_list()
            output.append(
                '<h3 style="font-size:16px;'
                'color:#29384f;margin:22px 0 10px;">'
                + _mail_inline(line[4:])
                + "</h3>"
            )
            continue

        if line.startswith("* ") or line.startswith("- "):
            if list_open != "ul":
                close_list()
                output.append(
                    '<ul style="color:#374151;'
                    'line-height:1.6;'
                    'padding-left:22px;">'
                )
                list_open = "ul"

            output.append(
                "<li>"
                + _mail_inline(line[2:])
                + "</li>"
            )
            continue

        numbered = re.match(
            r"^\d+\.\s+(.*)$",
            line,
        )

        if numbered:
            if list_open != "ol":
                close_list()
                output.append(
                    '<ol style="color:#374151;'
                    'line-height:1.6;'
                    'padding-left:22px;">'
                )
                list_open = "ol"

            output.append(
                "<li>"
                + _mail_inline(numbered.group(1))
                + "</li>"
            )
            continue

        close_list()

        output.append(
            '<p style="font-size:14px;'
            'line-height:1.65;'
            'color:#374151;'
            'margin:8px 0;">'
            + _mail_inline(line)
            + "</p>"
        )

    close_list()

    return "\n".join(output)


def _mail_charts(data):
    if not data:
        return {}

    try:
        import io
        import matplotlib

        matplotlib.use("Agg")

        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates

    except Exception:
        return {}

    charts = {}

    # --------------------------------------------------------
    # TEMPERATURE
    # --------------------------------------------------------

    indoor = _mail_series(data, "indoor_temp")
    outside = _mail_series(data, "outside_temp")
    boiler = _mail_series(data, "boiler_temp")

    if indoor or outside or boiler:
        fig, ax = plt.subplots(
            figsize=(9.2, 4.1),
            dpi=140,
        )

        if indoor:
            ax.plot(
                [t for t, _ in indoor],
                [v for _, v in indoor],
                label="Inde",
                linewidth=2.0,
            )

        if outside:
            ax.plot(
                [t for t, _ in outside],
                [v for _, v in outside],
                label="Ude",
                linewidth=1.8,
            )

        if boiler:
            ax.plot(
                [t for t, _ in boiler],
                [v for _, v in boiler],
                label="Kedel",
                linewidth=1.6,
            )

        ax.set_title(
            "Temperaturudvikling",
            loc="left",
            fontsize=14,
            fontweight="bold",
        )

        ax.set_ylabel("°C")
        ax.grid(axis="y", alpha=0.2)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)

        ax.legend(
            frameon=False,
            ncol=3,
            loc="upper left",
        )

        ax.xaxis.set_major_locator(
            mdates.AutoDateLocator(
                minticks=4,
                maxticks=8,
            )
        )

        ax.xaxis.set_major_formatter(
            mdates.DateFormatter("%d/%m")
        )

        fig.tight_layout()

        buffer = io.BytesIO()

        fig.savefig(
            buffer,
            format="png",
            bbox_inches="tight",
        )

        plt.close(fig)

        charts["temperature"] = buffer.getvalue()

    # --------------------------------------------------------
    # POWER
    # --------------------------------------------------------

    power = _mail_series(data, "power")

    if power:
        fig, ax = plt.subplots(
            figsize=(9.2, 3.2),
            dpi=140,
        )

        x = [t for t, _ in power]
        y = [v for _, v in power]

        ax.plot(
            x,
            y,
            linewidth=1.8,
        )

        ax.fill_between(
            x,
            y,
            0,
            alpha=0.2,
        )

        ax.set_title(
            "Brænderaktivitet",
            loc="left",
            fontsize=14,
            fontweight="bold",
        )

        ax.set_ylabel("%")
        ax.set_ylim(0, 100)
        ax.grid(axis="y", alpha=0.2)

        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)

        ax.xaxis.set_major_locator(
            mdates.AutoDateLocator(
                minticks=4,
                maxticks=8,
            )
        )

        ax.xaxis.set_major_formatter(
            mdates.DateFormatter("%d/%m")
        )

        fig.tight_layout()

        buffer = io.BytesIO()

        fig.savefig(
            buffer,
            format="png",
            bbox_inches="tight",
        )

        plt.close(fig)

        charts["power"] = buffer.getvalue()

    return charts


def _mail_html(
    report,
    data=None,
    model_used=None,
    fallback_used=False,
):
    import html

    indoor = _mail_average(
        data,
        "indoor_temp",
    )

    outside = _mail_average(
        data,
        "outside_temp",
    )

    target = (
        data.get("target_indoor_temp_c")
        if data
        else None
    )

    pellets = (
        data.get("feeder_consumption_kg")
        if data
        else None
    )

    cycles = (
        len(data.get("burner_cycles", []))
        if data
        else None
    )

    model = model_used or "Ikke oplyst"

    if fallback_used:
        model += " · fallback"

    report_html = _mail_report_html(report)

    return f'''<!DOCTYPE html>
<html>
<body style="
    margin:0;
    padding:0;
    background:#f1f4f7;
    font-family:Arial,sans-serif;
">

<table width="100%"
       cellpadding="0"
       cellspacing="0"
       style="background:#f1f4f7;">

<tr>
<td align="center"
    style="padding:24px 10px;">

<table width="100%"
       cellpadding="0"
       cellspacing="0"
       style="
           max-width:800px;
           background:#ffffff;
           border-radius:16px;
           overflow:hidden;
       ">

<tr>
<td style="
    background:#18222f;
    padding:30px 32px;
    color:white;
">

<div style="
    font-size:12px;
    letter-spacing:1.5px;
    color:#aeb9c6;
">
WOODY MONITOR
</div>

<div style="
    font-size:28px;
    font-weight:bold;
    margin-top:7px;
">
AI driftsanalyse
</div>

<div style="
    color:#c7d0db;
    font-size:14px;
    margin-top:8px;
">
{html.escape(_mail_period(data))}
</div>

</td>
</tr>

<tr>
<td style="padding:28px;">

<table width="100%"
       cellpadding="6"
       cellspacing="0">

<tr>

<td width="25%"
    valign="top"
    style="
        background:#f6f8fa;
        padding:15px;
        border-radius:10px;
    ">
<div style="font-size:11px;color:#7a8493;">
INDE
</div>
<div style="
    font-size:23px;
    font-weight:bold;
    margin-top:5px;
">
{_mail_value(indoor,1," °C")}
</div>
<div style="font-size:11px;color:#8d96a3;">
Mål {_mail_value(target,1," °C")}
</div>
</td>

<td width="25%"
    valign="top"
    style="
        background:#f6f8fa;
        padding:15px;
        border-radius:10px;
    ">
<div style="font-size:11px;color:#7a8493;">
UDE
</div>
<div style="
    font-size:23px;
    font-weight:bold;
    margin-top:5px;
">
{_mail_value(outside,1," °C")}
</div>
<div style="font-size:11px;color:#8d96a3;">
Gennemsnit
</div>
</td>

<td width="25%"
    valign="top"
    style="
        background:#f6f8fa;
        padding:15px;
        border-radius:10px;
    ">
<div style="font-size:11px;color:#7a8493;">
PILLER
</div>
<div style="
    font-size:23px;
    font-weight:bold;
    margin-top:5px;
">
{_mail_value(pellets,3," kg")}
</div>
<div style="font-size:11px;color:#8d96a3;">
Estimeret
</div>
</td>

<td width="25%"
    valign="top"
    style="
        background:#f6f8fa;
        padding:15px;
        border-radius:10px;
    ">
<div style="font-size:11px;color:#7a8493;">
CYKLUSSER
</div>
<div style="
    font-size:23px;
    font-weight:bold;
    margin-top:5px;
">
{cycles if cycles is not None else "–"}
</div>
<div style="font-size:11px;color:#8d96a3;">
Registreret
</div>
</td>

</tr>
</table>

<div style="
    margin-top:24px;
    border:1px solid #e7eaee;
    border-radius:12px;
    padding:14px;
">
<img
    src="cid:woody-temperature-chart"
    alt="Temperaturudvikling"
    style="width:100%;height:auto;display:block;"
>
</div>

<div style="
    margin-top:14px;
    border:1px solid #e7eaee;
    border-radius:12px;
    padding:14px;
">
<img
    src="cid:woody-power-chart"
    alt="Brænderaktivitet"
    style="width:100%;height:auto;display:block;"
>
</div>

<div style="
    margin-top:30px;
    font-size:11px;
    letter-spacing:1px;
    color:#7a8493;
">
AI-RAPPORT
</div>

{report_html}

<div style="
    background:#f6f8fa;
    border-radius:10px;
    padding:14px;
    margin-top:28px;
    font-size:11px;
    color:#727c8b;
    line-height:1.5;
">
<strong>Analysemodel:</strong>
{html.escape(model)}
<br>
Rapporten er rådgivning baseret på registrerede data.
Der er ikke foretaget automatiske ændringer af fyrets
styring eller sikkerhedsindstillinger.
</div>

</td>
</tr>

<tr>
<td style="
    text-align:center;
    color:#929ba7;
    font-size:11px;
    padding:20px;
">
Woody Monitor · AI energianalyse
</td>
</tr>

</table>
</td>
</tr>
</table>

</body>
</html>'''


def send_email(
    s,
    report,
    data=None,
    model_used=None,
    fallback_used=False,
):
    if (
        not s["email_to"]
        or not s["smtp_host"]
        or not s["smtp_from"]
    ):
        raise RuntimeError(
            "Udfyld modtager, SMTP-server og afsender."
        )

    from email.utils import parseaddr

    for address in (
        s["email_to"],
        s["smtp_from"],
    ):
        if (
            "\n" in address
            or "\r" in address
            or parseaddr(address)[1] != address
            or "@" not in address
        ):
            raise RuntimeError(
                "Ugyldig e-mailadresse."
            )

    if (
        s["smtp_username"]
        and not s["smtp_password"]
    ):
        raise RuntimeError(
            "SMTP-appadgangskode mangler."
        )

    msg = EmailMessage()

    msg["Subject"] = (
        "Woody Monitor – AI driftsanalyse"
    )

    msg["From"] = s["smtp_from"]
    msg["To"] = s["email_to"]

    # Almindelig tekstversion som fallback.
    msg.set_content(report)

    charts = _mail_charts(data)

    msg.add_alternative(
        _mail_html(
            report,
            data=data,
            model_used=model_used,
            fallback_used=fallback_used,
        ),
        subtype="html",
    )

    html_part = msg.get_payload()[-1]

    if charts.get("temperature"):
        html_part.add_related(
            charts["temperature"],
            maintype="image",
            subtype="png",
            cid="<woody-temperature-chart>",
            filename="temperature.png",
        )

    if charts.get("power"):
        html_part.add_related(
            charts["power"],
            maintype="image",
            subtype="png",
            cid="<woody-power-chart>",
            filename="burner-power.png",
        )

    security = s["smtp_security"]

    if security not in ("starttls", "ssl"):
        raise RuntimeError(
            "SMTP kræver STARTTLS eller SSL."
        )

    context = ssl.create_default_context()

    if security == "ssl":
        smtp = smtplib.SMTP_SSL(
            s["smtp_host"],
            int(s["smtp_port"]),
            timeout=30,
            context=context,
        )
    else:
        smtp = smtplib.SMTP(
            s["smtp_host"],
            int(s["smtp_port"]),
            timeout=30,
        )

    with smtp:
        smtp.ehlo()

        if security == "starttls":
            smtp.starttls(context=context)
            smtp.ehlo()

        if s["smtp_username"]:
            smtp.login(
                s["smtp_username"],
                s["smtp_password"],
            )

        smtp.send_message(msg)

def worker(s):
    global JOB
    try:
        d = payload(s["analysis_days"], s["target_indoor_temp"])
        with LOCK:
            JOB["message"] = "Gemini analyserer målingerne…"
        report, model_used, fallback_used = gemini(d, s)
        DATA.mkdir(parents=True, exist_ok=True)
        tmp = REPORT.with_suffix(".tmp")
        with open(tmp,"w") as f:
            os.fchmod(f.fileno(),0o600)
            f.write(report)
        os.replace(tmp, REPORT)
        os.chmod(REPORT,0o600)
        current = load()
        current["last_analysis"] = stamp()
        current["last_error"] = None
        current["last_model"] = model_used
        current["last_fallback_used"] = bool(fallback_used)
        save(current)
        if fallback_used:
            message = (
                f"Rapporten er gemt med fallback-model "
                f"{model_used}."
            )
        else:
            message = (
                f"Rapporten er gemt med {model_used}."
            )
        if s["email_enabled"]:
            try:
                send_email(
                    s,
                    report,
                    data=d,
                    model_used=model_used,
                    fallback_used=fallback_used,
                )
                current = load()
                current["last_email"] = stamp()
                save(current)
                message += " E-mail er sendt."
            except Exception:
                message += " E-mail kunne ikke sendes. Kontroller SMTP-opsætningen."
        with LOCK:
            JOB.update(state="completed", message=message, finished_at=stamp())
    except Exception as e:
        message = str(e)
        for secret in SECRET_FIELDS:
            if s.get(secret):message=message.replace(s[secret],"[redacted]")
        with LOCK:
            JOB.update(state="failed", message=message, finished_at=stamp())
        current = load()
        current["last_error"] = message
        save(current)

@router.get("/settings")
def get_settings():
    return {"ok": True, "settings": public(load())}

@router.put("/settings")
def update_settings(update: SettingsUpdate):
    changes = update.model_dump(exclude_none=True)
    if "smtp_security" in changes and changes["smtp_security"] not in ("starttls","ssl"):
        raise HTTPException(400, "SMTP kræver STARTTLS eller SSL.")
    if "gemini_model" in changes and not re.fullmatch(r"[A-Za-z0-9._-]+", changes["gemini_model"]):
        raise HTTPException(400, "Ugyldigt modelnavn.")
    with LOCK:
        d = load()
        for k,v in changes.items():
            if k in SECRET_FIELDS and not v:
                continue
            d[k] = v
        save(d)
    return {"ok": True, "settings": public(d)}

@router.get("/status")
def status():
    s = load()
    with LOCK:
        job = dict(JOB)
    return {"ok": True, "enabled": s["enabled"], "configured": bool(s["gemini_api_key"]),
            "email_enabled": s["email_enabled"], "last_analysis": s["last_analysis"],
            "last_email": s["last_email"], "last_error": s["last_error"],
            "last_model": s.get("last_model"),
            "last_fallback_used": s.get("last_fallback_used", False),
            "report_exists": REPORT.exists(), "job": job}

@router.get("/report")
def report():
    if not REPORT.exists():
        raise HTTPException(404, "Ingen rapport endnu.")
    return {"ok": True, "report": REPORT.read_text()}

@router.post("/analyze")
def analyze():
    global JOB
    with LOCK:
        s = load()
        if not s["enabled"] or not s["gemini_api_key"]:
            raise HTTPException(400, "Aktivér AI og gem Gemini-nøglen først.")
        if JOB["state"] == "running":
            raise HTTPException(409, "En analyse kører allerede.")
        JOB = {"state": "running", "message": "Indlæser historik…",
               "started_at": stamp(), "finished_at": None}
        threading.Thread(target=worker, args=(s,), daemon=True, name="woody-ai").start()
    return {"ok": True, "job": dict(JOB)}

@router.get("/models")
def models():
    s=load()
    if not s["gemini_api_key"]:raise HTTPException(400,"Gemini-nøglen mangler.")
    names=[]; page=None
    try:
        for _ in range(10):
            url="https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000"
            if page:
                from urllib.parse import quote
                url+="&pageToken="+quote(page,safe="")
            req=urllib.request.Request(url,headers={"x-goog-api-key":s["gemini_api_key"]})
            with urllib.request.urlopen(req,timeout=30) as response:d=json.load(response)
            names.extend(m["name"].removeprefix("models/") for m in d.get("models",[])
                         if "generateContent" in m.get("supportedGenerationMethods",[])
                         and "gemini" in m.get("name","").lower()
                         and not any(x in m.get("name","") for x in ("image","tts","transcribe","computer-use","robotics","omni")))
            page=d.get("nextPageToken")
            if not page:break
    except urllib.error.HTTPError as e:
        raise HTTPException(502,f"Google modeloversigt: HTTP {e.code}") from None
    except Exception:
        raise HTTPException(502,"Kunne ikke hente modeloversigten.") from None
    return {"ok":True,"models":sorted(set(names)),"selected":s["gemini_model"]}

@router.post("/email/test")
def test_email():
    s=load()
    try:
        send_email(s,"Dette er en testmail fra Woody Monitor. SMTP-opsætningen fungerer.")
    except Exception:
        raise HTTPException(502,"Testmail fejlede. Kontroller Gmail-adresse, appadgangskode, SMTP-server og TLS.") from None
    d=load();d["last_email"]=stamp();save(d)
    return {"ok":True,"message":"Testmail sendt. Kontroller din indbakke."}
