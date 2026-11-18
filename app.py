import base64
import gzip
import io
import json
import re
import zipfile
from datetime import datetime, timezone

import pandas as pd
import plotly.express as px
import requests
import streamlit as st

st.set_page_config(page_title="Финансовый дашборд ДЗ / ДО", page_icon="📊", layout="wide")


def get_secret(key, default=None):
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default


def get_github_cfg():
    try:
        gh = st.secrets.get("github", {})
        return {
            "token": gh.get("token", ""),
            "repo": gh.get("repo", ""),
            "path": gh.get("path", "data/published.json.gz"),
            "branch": gh.get("branch", "main"),
        }
    except Exception:
        return {"token": "", "repo": "", "path": "data/published.json.gz", "branch": "main"}


ADMIN_PASSWORD = get_secret("admin_password", "")
GITHUB = get_github_cfg()
PUBLISH_ENABLED = bool(GITHUB["token"] and GITHUB["repo"])


def _gh_headers():
    return {
        "Authorization": f"Bearer {GITHUB['token']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def load_published_snapshot():
    if not PUBLISH_ENABLED:
        return None
    api_url = (
        f"https://api.github.com/repos/{GITHUB['repo']}/contents/{GITHUB['path']}"
        f"?ref={GITHUB['branch']}"
    )
    try:
        r = requests.get(api_url, headers=_gh_headers(), timeout=60)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            st.warning(f"GitHub API {r.status_code}: {r.text[:400]}")
            return None
        meta = r.json()
        raw_bytes = None
        if meta.get("content"):
            raw_bytes = base64.b64decode(meta["content"])
        elif meta.get("download_url"):
            r2 = requests.get(
                meta["download_url"],
                headers={"Authorization": f"Bearer {GITHUB['token']}"},
                timeout=180,
            )
            if r2.status_code != 200:
                st.warning(f"download_url HTTP {r2.status_code}: {r2.text[:300]}")
                return None
            raw_bytes = r2.content
        elif meta.get("sha"):
            blob_url = f"https://api.github.com/repos/{GITHUB['repo']}/git/blobs/{meta['sha']}"
            r3 = requests.get(blob_url, headers=_gh_headers(), timeout=180)
            if r3.status_code == 200:
                blob = r3.json()
                if blob.get("content"):
                    raw_bytes = base64.b64decode(blob["content"])
        if not raw_bytes:
            st.warning("Опубликованный файл пустой. Опубликуйте данные заново.")
            return None
        try:
            text = gzip.decompress(raw_bytes).decode("utf-8")
        except OSError:
            text = raw_bytes.decode("utf-8")
        text = text.strip()
        if not text:
            return None
        return json.loads(text)
    except Exception as e:
        st.warning(f"Не удалось загрузить опубликованные данные: {e}")
        return None


def publish_snapshot(snapshot: dict):
    if not PUBLISH_ENABLED:
        return False, "Публикация не настроена (нет token/repo в secrets)."
    url = f"https://api.github.com/repos/{GITHUB['repo']}/contents/{GITHUB['path']}"
    sha = None
    try:
        r = requests.get(url + f"?ref={GITHUB['branch']}", headers=_gh_headers(), timeout=30)
        if r.status_code == 200:
            sha = r.json().get("sha")
    except Exception:
        pass
    raw = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    compressed = gzip.compress(raw, compresslevel=9)
    if len(compressed) > 90 * 1024 * 1024:
        return False, f"Снимок слишком большой ({len(compressed)/1024/1024:.1f} МБ)."
    payload = {
        "message": f"Публикация снимка ДЗ/ДО {snapshot.get('published_at', '')}",
        "content": base64.b64encode(compressed).decode("ascii"),
        "branch": GITHUB["branch"],
    }
    if sha:
        payload["sha"] = sha
    try:
        r = requests.put(url, headers=_gh_headers(), json=payload, timeout=180)
        if r.status_code in (200, 201):
            return True, f"Опубликовано: {len(raw)/1024/1024:.1f} МБ → {len(compressed)/1024:.0f} КБ (gzip)."
        return False, f"Ошибка GitHub API ({r.status_code}): {r.text[:500]}"
    except Exception as e:
        return False, str(e)


def _dt_str(v):
    if v is None or (isinstance(v, float) and pd.isna(v)) or pd.isna(v):
        return None
    try:
        return pd.Timestamp(v).strftime("%Y-%m-%d")
    except Exception:
        return None


def isocal_week_label(dt):
    if pd.isna(dt):
        return None
    iso = pd.Timestamp(dt).isocalendar()
    return f"{int(iso.year)}-W{int(iso.week):02d}"


def current_iso_week_label():
    today = pd.Timestamp.now().normalize()
    iso = today.isocalendar()
    return f"{int(iso.year)}-W{int(iso.week):02d}"


def exclude_client_from_unissued(name: str) -> bool:
    s = str(name or "")
    s_low = s.lower().replace("«", '"').replace("»", '"').replace("“", '"').replace("”", '"')
    if re.search(r"(^|[\s\"'])ип([\s\"'\.]|$)", s_low):
        return True
    if "взлет" in s_low or "взлёт" in s_low:
        return True
    return False


def ensure_dz_categories(df: pd.DataFrame) -> pd.DataFrame:
    """Гарантирует наличие расчётных колонок (важно для старых снимков)."""
    if df is None or df.empty:
        return df if df is not None else pd.DataFrame()

    df = df.copy()
    if "Сумма счета" not in df.columns:
        df["Сумма счета"] = 0.0
    if "Оплачено" not in df.columns:
        df["Оплачено"] = 0.0
    if "Просрочка, дни" not in df.columns:
        df["Просрочка, дни"] = 0.0

    for c in ("Сумма счета", "Оплачено", "Просрочка, дни"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    if "Остаток" not in df.columns:
        df["Остаток"] = (df["Сумма счета"] - df["Оплачено"]).clip(lower=0)
    else:
        df["Остаток"] = pd.to_numeric(df["Остаток"], errors="coerce").fillna(0)

    if "Дата оплаты по договору" in df.columns:
        df["Дата оплаты по договору"] = pd.to_datetime(df["Дата оплаты по договору"], errors="coerce")
    else:
        df["Дата оплаты по договору"] = pd.NaT

    if "Дата отправления" in df.columns:
        df["Дата отправления"] = pd.to_datetime(df["Дата отправления"], errors="coerce")
    else:
        df["Дата отправления"] = pd.NaT

    is_overdue = df["Просрочка, дни"] < 0
    has_underpay = df["Остаток"] > 0
    has_pdo = df["Дата оплаты по договору"].notna()
    no_sent = df["Дата отправления"].isna()

    df["Просрочено"] = df["Остаток"].where(is_overdue & has_underpay, 0)
    df["Есть в ДЗ"] = df["Остаток"].where(has_pdo & (df["Просрочка, дни"] >= 0), 0)
    df["Нет ПДО"] = df["Сумма счета"].where(no_sent, 0)
    df["Не отправлено"] = df["Остаток"].where(no_sent, 0)

    cur_week = current_iso_week_label()
    week_labels = df["Дата оплаты по договору"].map(isocal_week_label)
    due_this_week = (week_labels == cur_week) & (df["Остаток"] > 0)
    df["Подходит к оплате"] = df["Остаток"].where(due_this_week | (is_overdue & has_underpay), 0)

    return df


def ensure_unissued_categories(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df if df is not None else pd.DataFrame()
    df = df.copy()
    if "Сумма" not in df.columns:
        df["Сумма"] = 0.0
    df["Сумма"] = pd.to_numeric(df["Сумма"], errors="coerce").fillna(0)

    if "Номер счета" not in df.columns:
        df["Номер счета"] = ""
    inv = df["Номер счета"].astype(str).str.strip()
    no_invoice = inv.isna() | (inv == "") | (inv.str.lower() == "nan") | (inv.str.lower() == "none")

    if "Дата оплаты клиента" in df.columns:
        df["Дата оплаты клиента"] = pd.to_datetime(df["Дата оплаты клиента"], errors="coerce")
    else:
        df["Дата оплаты клиента"] = pd.NaT
    no_client_pay = df["Дата оплаты клиента"].isna()

    if "Контрагент" not in df.columns:
        df["Контрагент"] = ""
    excluded = df["Контрагент"].map(exclude_client_from_unissued)

    df["Не отправлено"] = df["Сумма"].where(no_invoice, 0)
    df["Не выставлено"] = df["Сумма"].where(no_invoice & no_client_pay & ~excluded, 0)
    return df


def snapshot_from_frames(dz: pd.DataFrame, unissued: pd.DataFrame) -> dict:
    dz = ensure_dz_categories(dz)
    unissued = ensure_unissued_categories(unissued)
    dz_cols = [
        "Контрагент", "Плательщик", "Заявка", "Номер счета",
        "Дата счета", "Дата оплаты по договору", "Просрочка, дни",
        "Сумма счета", "Оплачено", "Остаток",
        "Просрочено", "Есть в ДЗ", "Подходит к оплате", "Не отправлено", "Нет ПДО",
        "Дата отправления",
    ]
    dz_out = dz[[c for c in dz_cols if c in dz.columns]].copy()
    for c in ("Дата счета", "Дата оплаты по договору", "Дата отправления"):
        if c in dz_out.columns:
            dz_out[c] = dz_out[c].map(_dt_str)
    for c in dz_out.select_dtypes(include="float").columns:
        dz_out[c] = dz_out[c].round(2)

    un_cols = [
        "Контрагент", "Заявка", "Сумма", "Дата загрузки", "Дата заявки",
        "Дата оплаты клиента", "Бухгалтер", "Не выставлено", "Не отправлено", "Номер счета",
    ]
    un_out = unissued[[c for c in un_cols if c in unissued.columns]].copy()
    for c in ("Дата загрузки", "Дата заявки", "Дата оплаты клиента"):
        if c in un_out.columns:
            un_out[c] = un_out[c].map(_dt_str)
    for c in un_out.select_dtypes(include="float").columns:
        un_out[c] = un_out[c].round(2)

    return {
        "version": 4,
        "published_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "dz": json.loads(dz_out.to_json(orient="records", force_ascii=False)),
        "unissued": json.loads(un_out.to_json(orient="records", force_ascii=False)),
    }


def frames_from_snapshot(snap: dict):
    dz = pd.DataFrame(snap.get("dz", []))
    unissued = pd.DataFrame(snap.get("unissued", []))
    for c in ("Дата счета", "Дата оплаты по договору", "Дата отправления"):
        if c in dz.columns:
            dz[c] = pd.to_datetime(dz[c], errors="coerce")
    for c in ("Сумма счета", "Оплачено", "Остаток", "Просрочка, дни"):
        if c in dz.columns:
            dz[c] = pd.to_numeric(dz[c], errors="coerce").fillna(0)
    for c in ("Дата загрузки", "Дата заявки", "Дата оплаты клиента"):
        if c in unissued.columns:
            unissued[c] = pd.to_datetime(unissued[c], errors="coerce")
    if "Сумма" in unissued.columns:
        unissued["Сумма"] = pd.to_numeric(unissued["Сумма"], errors="coerce").fillna(0)
    # Пересчёт категорий — работает и со старыми снимками
    dz = ensure_dz_categories(dz)
    unissued = ensure_unissued_categories(unissued)
    return dz, unissued


def normalize_xlsx(file_bytes: bytes) -> io.BytesIO:
    src = io.BytesIO(file_bytes)
    dst = io.BytesIO()
    with zipfile.ZipFile(src, "r") as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            name = item.filename
            if name.lower() == "xl/sharedstrings.xml":
                name = "xl/sharedStrings.xml"
            zout.writestr(name, zin.read(item.filename))
    dst.seek(0)
    return dst


def read_source(uploaded_file, header_row=4):
    return pd.read_excel(normalize_xlsx(uploaded_file.getvalue()), header=header_row, engine="openpyxl")


def find_col(df, *candidates):
    normalized = {str(c).strip().lower(): c for c in df.columns}
    for candidate in candidates:
        key = candidate.strip().lower()
        if key in normalized:
            return normalized[key]
    return None


def to_date(series):
    return pd.to_datetime(series, errors="coerce", dayfirst=True)


def extract_invoice_date(value):
    if pd.isna(value):
        return pd.NaT
    match = re.search(r"\bот\s+(\d{2}\.\d{2}\.\d{4})", str(value), flags=re.I)
    if not match:
        match = re.search(r"(\d{2}\.\d{2}\.\d{4})", str(value))
    return pd.to_datetime(match.group(1), dayfirst=True, errors="coerce") if match else pd.NaT


def number(series):
    return pd.to_numeric(
        series.astype(str)
        .str.replace("\u00a0", "", regex=False)
        .str.replace(" ", "", regex=False)
        .str.replace(",", ".", regex=False),
        errors="coerce",
    ).fillna(0)


def rub(value):
    try:
        return f"{float(value):,.2f} ₽".replace(",", " ").replace(".", ",")
    except Exception:
        return "—"


def build_dz(raw):
    c_payer = find_col(raw, "Плательщик")
    c_client = find_col(raw, "Заявка.Контрагент", "Контрагент")
    c_request = find_col(raw, "Заявка.Номер", "Номер заявки")
    c_invoice = find_col(raw, "Счет на оплату", "Счёт на оплату")
    c_due = find_col(raw, "Дата оплаты по договору")
    c_late = find_col(raw, "Количество дней просрочки счета", "Количество дней просрочки счёта")
    c_invoice_sum = find_col(raw, "Сумма по счету", "Сумма по счёту")
    c_paid = find_col(raw, "Сумма оплаченная")
    c_request_sum = find_col(raw, "Сумма по заявке")
    c_sent = find_col(raw, "Дата отправления")

    if c_client is None or c_invoice is None:
        raise ValueError("В файле ДЗ не найдены «Заявка.Контрагент» и/или «Счет на оплату».")
    if c_invoice_sum is None:
        raise ValueError("В файле ДЗ не найден столбец «Сумма по счету».")

    invoice_text = raw[c_invoice]
    invoice_no = invoice_text.astype(str).str.extract(r"(\d+)", expand=False)

    df = pd.DataFrame({
        "Контрагент": raw[c_client].astype(str).str.strip(),
        "Плательщик": raw[c_payer].astype(str).str.strip() if c_payer else "",
        "Заявка": raw[c_request] if c_request else "",
        "Номер счета": invoice_no,
        "Дата счета": invoice_text.map(extract_invoice_date),
        "Дата оплаты по договору": to_date(raw[c_due]) if c_due else pd.NaT,
        "Просрочка, дни": number(raw[c_late]) if c_late else 0,
        "Сумма счета": number(raw[c_invoice_sum]),
        "Сумма по заявке": number(raw[c_request_sum]) if c_request_sum else 0,
        "Оплачено": number(raw[c_paid]) if c_paid else 0,
        "Дата отправления": to_date(raw[c_sent]) if c_sent else pd.NaT,
        "Исходный документ": invoice_text.astype(str),
    })
    df = df[
        (df["Контрагент"] != "")
        & (df["Контрагент"].str.lower() != "nan")
        & (df["Контрагент"].str.lower() != "none")
        & (df["Контрагент"].str.lower() != "итого")
        & (df["Сумма счета"] != 0)
    ].copy()
    return ensure_dz_categories(df)


def build_unissued(raw_do):
    c_client = find_col(raw_do, "Клиент")
    c_request = find_col(raw_do, "Заявка")
    c_invoice = find_col(raw_do, "Номер счета", "Номер счёта")
    c_amount = find_col(raw_do, "Ставка заказчика")
    c_load = find_col(raw_do, "Дата загрузки")
    c_date = find_col(raw_do, "Дата")
    c_paid_date = find_col(raw_do, "Дата оплаты клиента")
    c_manager = find_col(raw_do, "Клиентский бухгалтер", "Менеджер")

    if c_client is None or c_amount is None:
        raise ValueError("В файле ДО не найдены «Клиент» и/или «Ставка заказчика».")

    amount = number(raw_do[c_amount])
    invoice = raw_do[c_invoice].astype(str).str.strip() if c_invoice else pd.Series([""] * len(raw_do))
    paid_date = to_date(raw_do[c_paid_date]) if c_paid_date else pd.Series(pd.NaT, index=raw_do.index)

    df = pd.DataFrame({
        "Контрагент": raw_do[c_client].astype(str).str.strip(),
        "Заявка": raw_do[c_request] if c_request else "",
        "Номер счета": invoice,
        "Сумма": amount,
        "Дата загрузки": to_date(raw_do[c_load]) if c_load else pd.NaT,
        "Дата заявки": to_date(raw_do[c_date]) if c_date else pd.NaT,
        "Дата оплаты клиента": paid_date,
        "Бухгалтер": raw_do[c_manager].astype(str).str.strip() if c_manager else "",
    })
    df = df[
        (df["Контрагент"] != "")
        & (df["Контрагент"].str.lower() != "nan")
        & (df["Сумма"] != 0)
    ].copy()
    return ensure_unissued_categories(df)


def build_summary(dz, unissued):
    dz = ensure_dz_categories(dz) if dz is not None else pd.DataFrame()
    unissued = ensure_unissued_categories(unissued) if unissued is not None else pd.DataFrame()

    if dz is None or dz.empty:
        issued = pd.DataFrame(columns=[
            "Контрагент", "Выставлено", "Оплачено", "Неоплачено", "Просрочено",
            "Есть в ДЗ", "Подходит к оплате", "Не отправлено_ДЗ", "Нет ПДО",
        ])
    else:
        # Только существующие колонки — защита от KeyError
        agg_map = {}
        wanted = {
            "Выставлено": "Сумма счета",
            "Оплачено": "Оплачено",
            "Неоплачено": "Остаток",
            "Просрочено": "Просрочено",
            "Есть в ДЗ": "Есть в ДЗ",
            "Подходит к оплате": "Подходит к оплате",
            "Не отправлено_ДЗ": "Не отправлено",
            "Нет ПДО": "Нет ПДО",
        }
        for out_name, col in wanted.items():
            if col in dz.columns:
                agg_map[out_name] = (col, "sum")
        if "Контрагент" not in dz.columns:
            issued = pd.DataFrame(columns=list(wanted.keys()) + ["Контрагент"])
        else:
            issued = dz.groupby("Контрагент", as_index=False).agg(**agg_map)

    if unissued is None or unissued.empty:
        from_do = pd.DataFrame(columns=["Контрагент", "Не выставлено", "Не отправлено_ДО"])
    else:
        agg_do = {}
        if "Не выставлено" in unissued.columns:
            agg_do["Не выставлено"] = ("Не выставлено", "sum")
        if "Не отправлено" in unissued.columns:
            agg_do["Не отправлено_ДО"] = ("Не отправлено", "sum")
        if agg_do and "Контрагент" in unissued.columns:
            from_do = unissued.groupby("Контрагент", as_index=False).agg(**agg_do)
        else:
            from_do = pd.DataFrame(columns=["Контрагент", "Не выставлено", "Не отправлено_ДО"])

    summary = issued.merge(from_do, on="Контрагент", how="outer").fillna(0)

    for c in [
        "Выставлено", "Оплачено", "Неоплачено", "Просрочено", "Есть в ДЗ",
        "Подходит к оплате", "Не отправлено_ДЗ", "Не отправлено_ДО", "Нет ПДО", "Не выставлено",
    ]:
        if c not in summary.columns:
            summary[c] = 0.0
        summary[c] = pd.to_numeric(summary[c], errors="coerce").fillna(0)

    summary["Не отправлено"] = summary["Не отправлено_ДЗ"] + summary["Не отправлено_ДО"]
    summary["Оборот"] = summary["Выставлено"] + summary["Не выставлено"]
    summary["Задолженность"] = summary["Оборот"] - summary["Выставлено"]

    cols = [
        "Контрагент", "Оборот", "Выставлено", "Оплачено", "Неоплачено", "Просрочено",
        "Есть в ДЗ", "Подходит к оплате", "Не отправлено", "Нет ПДО", "Не выставлено", "Задолженность",
    ]
    summary = summary[cols]
    return summary.sort_values("Оборот", ascending=False).reset_index(drop=True)


def build_payment_plan(dz):
    dz = ensure_dz_categories(dz) if dz is not None else pd.DataFrame()
    if dz is None or dz.empty:
        return pd.DataFrame()

    overdue = dz.groupby("Контрагент", as_index=True)["Просрочено"].sum().rename("Просрочка")

    plan = dz[(dz["Остаток"] > 0) & dz["Дата оплаты по договору"].notna()].copy()
    if plan.empty:
        if overdue.sum() == 0:
            return pd.DataFrame()
        result = overdue.to_frame()
        result = result[result["Просрочка"] > 0].sort_values("Просрочка", ascending=False)
        total = pd.DataFrame({"Просрочка": [result["Просрочка"].sum()]}, index=["ИТОГО"])
        return pd.concat([total, result])

    plan["Неделя"] = plan["Дата оплаты по договору"].map(isocal_week_label)
    pivot = (
        plan.groupby(["Контрагент", "Неделя"], as_index=False)["Остаток"].sum()
        .pivot(index="Контрагент", columns="Неделя", values="Остаток")
        .fillna(0)
    )
    week_cols = sorted([c for c in pivot.columns if c is not None])
    pivot = pivot[week_cols]
    pivot = pivot.join(overdue, how="left")
    pivot["Просрочка"] = pivot["Просрочка"].fillna(0)

    only_overdue = overdue[~overdue.index.isin(pivot.index)]
    only_overdue = only_overdue[only_overdue > 0]
    if not only_overdue.empty:
        extra = pd.DataFrame(0.0, index=only_overdue.index, columns=week_cols)
        extra["Просрочка"] = only_overdue
        pivot = pd.concat([pivot, extra])

    pivot["Итого"] = pivot[week_cols].sum(axis=1) if week_cols else 0
    ordered = ["Просрочка"] + week_cols + ["Итого"]
    pivot = pivot[ordered].sort_values("Итого", ascending=False)

    total_row = pivot.sum(axis=0).to_frame().T
    total_row.index = ["ИТОГО"]
    return pd.concat([total_row, pivot])


def metrics_from_summary(summary_f):
    keys = [
        "Оборот", "Выставлено", "Оплачено", "Неоплачено", "Просрочено",
        "Есть в ДЗ", "Подходит к оплате", "Не отправлено", "Нет ПДО",
        "Не выставлено", "Задолженность",
    ]
    if summary_f is None or summary_f.empty:
        return {k: 0.0 for k in keys}
    out = {}
    for k in keys:
        if k in summary_f.columns:
            out[k] = float(pd.to_numeric(summary_f[k], errors="coerce").fillna(0).sum())
        else:
            out[k] = 0.0
    return out


def apply_filters(dz, unissued, clients, payers, invoice_range):
    dz_f = dz.copy() if dz is not None else pd.DataFrame()
    un_f = unissued.copy() if unissued is not None else pd.DataFrame()
    if clients:
        if not dz_f.empty and "Контрагент" in dz_f.columns:
            dz_f = dz_f[dz_f["Контрагент"].isin(clients)]
        if not un_f.empty and "Контрагент" in un_f.columns:
            un_f = un_f[un_f["Контрагент"].isin(clients)]
    if payers and not dz_f.empty and "Плательщик" in dz_f.columns:
        dz_f = dz_f[dz_f["Плательщик"].isin(payers)]
    if invoice_range and len(invoice_range) == 2 and not dz_f.empty and "Дата счета" in dz_f.columns:
        start = pd.Timestamp(invoice_range[0])
        end = pd.Timestamp(invoice_range[1]) + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
        dz_f = dz_f[dz_f["Дата счета"].between(start, end)]
    return dz_f, un_f


def render_kpi(metrics):
    order = [
        ("Оборот", "Оборот"),
        ("Выставлено", "Выставлено (Сумма по счету)"),
        ("Оплачено", "Оплачено"),
        ("Неоплачено", "Неоплачено"),
        ("Просрочено", "Просрочено"),
        ("Есть в ДЗ", "Есть в ДЗ"),
        ("Подходит к оплате", "Подходит к оплате"),
        ("Не отправлено", "Не отправлено"),
        ("Нет ПДО", "Нет ПДО"),
        ("Не выставлено", "Не выставлено"),
        ("Задолженность", "Общая сумма задолженности"),
    ]
    for row in (order[0:4], order[4:8], order[8:11]):
        cols = st.columns(len(row))
        for col, (key, label) in zip(cols, row):
            col.metric(label, rub(metrics.get(key, 0)))


def render_dashboard(summary_f, metrics, plan_df, dz_f, unissued_f, source_label=""):
    if source_label:
        st.caption(source_label)
    render_kpi(metrics)

    tabs = st.tabs([
        "📋 Сводка по контрагентам",
        "📅 Планирование оплат",
        "📈 Аналитика",
        "🔎 Детализация",
        "⬇️ Выгрузка",
    ])

    with tabs[0]:
        st.subheader("Сводка в формате «ОБЩАЯ ДЗ»")
        if summary_f is not None and not summary_f.empty:
            show = summary_f.drop(columns=["Задолженность"], errors="ignore")
            fmt = {c: "{:,.2f}" for c in show.columns if c != "Контрагент"}
            st.dataframe(show.style.format(fmt), use_container_width=True, hide_index=True, height=520)
        else:
            st.info("Нет данных.")

    with tabs[1]:
        st.subheader("Планирование оплат по неделям")
        st.caption("Строка ИТОГО — суммы по неделям. Столбец «Просрочка» — просроченные остатки.")
        if plan_df is None or plan_df.empty:
            st.info("Нет данных для плана оплат.")
        else:
            st.dataframe(plan_df.style.format("{:,.2f}"), use_container_width=True, height=520)

    with tabs[2]:
        left, right = st.columns(2)
        with left:
            status = pd.DataFrame({
                "Категория": [
                    "Оплачено", "Неоплачено", "Просрочено", "Есть в ДЗ",
                    "Подходит к оплате", "Не отправлено", "Нет ПДО", "Не выставлено",
                ],
                "Сумма": [
                    metrics.get("Оплачено", 0), metrics.get("Неоплачено", 0),
                    metrics.get("Просрочено", 0), metrics.get("Есть в ДЗ", 0),
                    metrics.get("Подходит к оплате", 0), metrics.get("Не отправлено", 0),
                    metrics.get("Нет ПДО", 0), metrics.get("Не выставлено", 0),
                ],
            })
            status = status[status["Сумма"] > 0]
            if not status.empty:
                fig = px.pie(status, names="Категория", values="Сумма", hole=0.5, title="Структура")
                st.plotly_chart(fig, use_container_width=True)
        with right:
            bar_df = pd.DataFrame({
                "Показатель": [
                    "Оборот", "Выставлено", "Оплачено", "Неоплачено", "Просрочено",
                    "Есть в ДЗ", "Подходит к оплате", "Не отправлено", "Нет ПДО", "Не выставлено",
                ],
                "Сумма": [
                    metrics.get("Оборот", 0), metrics.get("Выставлено", 0),
                    metrics.get("Оплачено", 0), metrics.get("Неоплачено", 0),
                    metrics.get("Просрочено", 0), metrics.get("Есть в ДЗ", 0),
                    metrics.get("Подходит к оплате", 0), metrics.get("Не отправлено", 0),
                    metrics.get("Нет ПДО", 0), metrics.get("Не выставлено", 0),
                ],
            })
            fig = px.bar(bar_df, x="Показатель", y="Сумма", title="Ключевые показатели",
                         labels={"Сумма": "Сумма, ₽"})
            st.plotly_chart(fig, use_container_width=True)

        if summary_f is not None and not summary_f.empty and "Оборот" in summary_f.columns:
            tmp = summary_f.copy()
            tmp["Оборот"] = pd.to_numeric(tmp["Оборот"], errors="coerce").fillna(0)
            n = min(15, len(tmp))
            if n > 0:
                top = tmp.nlargest(n, "Оборот")
                fig = px.bar(
                    top.sort_values("Оборот"), x="Оборот", y="Контрагент", orientation="h",
                    title="Топ контрагентов по обороту", labels={"Оборот": "Оборот, ₽"},
                )
                st.plotly_chart(fig, use_container_width=True)

        if dz_f is not None and not dz_f.empty and "Дата счета" in dz_f.columns and dz_f["Дата счета"].notna().any():
            monthly = dz_f.dropna(subset=["Дата счета"]).copy()
            monthly["Месяц"] = monthly["Дата счета"].dt.to_period("M").dt.to_timestamp()
            monthly = monthly.groupby("Месяц", as_index=False).agg(
                Выставлено=("Сумма счета", "sum"),
                Оплачено=("Оплачено", "sum"),
                Просрочено=("Просрочено", "sum"),
            )
            fig = px.line(
                monthly, x="Месяц", y=["Выставлено", "Оплачено", "Просрочено"], markers=True,
                title="Динамика по месяцам", labels={"value": "Сумма, ₽", "variable": "Показатель"},
            )
            st.plotly_chart(fig, use_container_width=True)

    with tabs[3]:
        if dz_f is not None and not dz_f.empty:
            st.subheader("Детализация счетов (ДЗ)")
            search = st.text_input("Поиск по контрагенту, счёту или заявке", key="search_dz")
            view = dz_f
            if search:
                mask = (
                    view["Контрагент"].astype(str).str.contains(search, case=False, na=False)
                    | view["Номер счета"].astype(str).str.contains(search, case=False, na=False)
                    | view["Заявка"].astype(str).str.contains(search, case=False, na=False)
                )
                view = view[mask]
            cols = [c for c in [
                "Контрагент", "Плательщик", "Заявка", "Номер счета", "Дата счета",
                "Дата оплаты по договору", "Просрочка, дни", "Сумма счета", "Оплачено",
                "Остаток", "Просрочено", "Есть в ДЗ", "Подходит к оплате",
                "Не отправлено", "Нет ПДО", "Дата отправления",
            ] if c in view.columns]
            sort_cols = [c for c in ["Дата счета", "Контрагент"] if c in view.columns]
            if sort_cols:
                view = view.sort_values(sort_cols, ascending=[False] + [True] * (len(sort_cols) - 1))
            st.dataframe(view[cols], use_container_width=True, hide_index=True, height=420)
        else:
            st.info("Нет детализации ДЗ.")

        if unissued_f is not None and not unissued_f.empty:
            st.subheader("Заявки из ДО")
            st.dataframe(
                unissued_f.sort_values("Сумма", ascending=False),
                use_container_width=True, hide_index=True, height=320,
            )

    with tabs[4]:
        if summary_f is not None and not summary_f.empty:
            csv = summary_f.to_csv(index=False, sep=";", decimal=",", encoding="utf-8-sig").encode("utf-8-sig")
            st.download_button("Скачать сводку (CSV)", data=csv, file_name="сводка_ДЗ.csv", mime="text/csv")
            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                summary_f.to_excel(writer, index=False, sheet_name="ОБЩАЯ ДЗ")
                if plan_df is not None and not plan_df.empty:
                    plan_df.reset_index().to_excel(writer, index=False, sheet_name="План оплат")
                if dz_f is not None and not dz_f.empty:
                    dz_f.to_excel(writer, index=False, sheet_name="Детализация ДЗ")
                if unissued_f is not None and not unissued_f.empty:
                    unissued_f.to_excel(writer, index=False, sheet_name="ДО")
            st.download_button(
                "Скачать Excel", data=buf.getvalue(), file_name="Отчет_ДЗ_ДО.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )


# =============================================================================
st.title("📊 Финансовый дашборд ДЗ / ДО")

with st.sidebar:
    st.header("Режим")
    is_admin = False
    if ADMIN_PASSWORD:
        pwd = st.text_input("Пароль администратора", type="password", key="admin_pwd")
        is_admin = pwd == ADMIN_PASSWORD
        if pwd and not is_admin:
            st.error("Неверный пароль")
    else:
        is_admin = st.checkbox("Режим загрузки и публикации", value=False)

    dz_file = do_file = None
    if is_admin:
        st.success("Режим администратора")
        st.divider()
        st.header("Загрузка данных")
        dz_file = st.file_uploader("Файл ДЗ (.xlsx)", type=["xlsx"], key="dz")
        do_file = st.file_uploader("Файл ДО (.xlsx)", type=["xlsx"], key="do")

base_dz = base_unissued = None
source_mode = None

if is_admin and dz_file and do_file:
    try:
        base_dz = build_dz(read_source(dz_file, header_row=4))
        base_unissued = build_unissued(read_source(do_file, header_row=4))
        source_mode = "live"
    except Exception as exc:
        st.error(f"Не удалось обработать файлы: {exc}")

if base_dz is None:
    snap = load_published_snapshot()
    if snap is not None and snap.get("dz") is not None:
        base_dz, base_unissued = frames_from_snapshot(snap)
        source_mode = "published"
        st.sidebar.caption(f"Опубликовано: {snap.get('published_at', '—')}")
    elif snap is not None:
        st.warning("Снимок старого формата. Загрузите файлы и опубликуйте заново.")
        st.stop()

if base_dz is None:
    st.info(
        "Нет данных.\n\n"
        "Администратор: пароль → дз.xlsx + до.xlsx → «Опубликовать для руководства»."
    )
    st.stop()

# На всякий случай пересчитываем категории
base_dz = ensure_dz_categories(base_dz)
base_unissued = ensure_unissued_categories(base_unissued)

with st.sidebar:
    st.divider()
    st.header("Фильтры")
    all_clients = set(base_dz["Контрагент"].dropna().unique().tolist()) if "Контрагент" in base_dz.columns else set()
    if base_unissued is not None and not base_unissued.empty and "Контрагент" in base_unissued.columns:
        all_clients |= set(base_unissued["Контрагент"].dropna().unique().tolist())
    clients = sorted(all_clients)
    selected_clients = st.multiselect("Контрагент", clients, default=[])

    payers = []
    if "Плательщик" in base_dz.columns:
        payers = sorted([
            x for x in base_dz["Плательщик"].dropna().unique().tolist()
            if x and str(x).lower() not in ("nan", "none")
        ])
    selected_payers = st.multiselect("Плательщик", payers, default=[]) if payers else []

    invoice_range = None
    if "Дата счета" in base_dz.columns:
        valid_inv = base_dz["Дата счета"].dropna()
        if not valid_inv.empty:
            min_d, max_d = valid_inv.min().date(), valid_inv.max().date()
            invoice_range = st.date_input(
                "Дата счёта", value=(min_d, max_d), min_value=min_d, max_value=max_d, key="invoice_date",
            )

dz_f, unissued_f = apply_filters(base_dz, base_unissued, selected_clients, selected_payers, invoice_range)
summary_f = build_summary(dz_f, unissued_f)
metrics = metrics_from_summary(summary_f)
plan_df = build_payment_plan(dz_f)

st.sidebar.caption(
    f"Строк ДЗ: {len(dz_f):,} · заявок ДО: {len(unissued_f):,}".replace(",", " ")
)

if is_admin and source_mode == "live":
    st.sidebar.divider()
    if not PUBLISH_ENABLED:
        st.sidebar.warning("Публикация не настроена (secrets).")
    elif st.sidebar.button("📢 Опубликовать для руководства", type="primary"):
        with st.sidebar:
            with st.spinner("Сжатие и загрузка снимка на GitHub…"):
                snap = snapshot_from_frames(base_dz, base_unissued)
                ok, msg = publish_snapshot(snap)
            if ok:
                st.success(msg)
                st.balloons()
            else:
                st.error(msg)

label = "Данные из загруженных файлов" if source_mode == "live" else "Опубликованные данные"
render_dashboard(summary_f, metrics, plan_df, dz_f, unissued_f, source_label=label)

with st.expander("Пояснения к расчётам"):
    st.markdown(
        f"""
**Порядок:** Оборот → Выставлено → Оплачено → Неоплачено → Просрочено → Есть в ДЗ →  
Подходит к оплате → Не отправлено → Нет ПДО → Не выставлено  
(+ **Общая сумма задолженности** = Оборот − Выставлено)

| Показатель | Правило |
|---|---|
| **Просрочено** | дни просрочки **&lt; 0** и есть недоплата |
| **Есть в ДЗ** | есть ПДО и дни просрочки **≥ 0** (остаток) |
| **Подходит к оплате** | остаток с оплатой на текущую неделю (**{current_iso_week_label()}**) + Просрочено |
| **Не отправлено** | ДО без счёта + ДЗ без даты отправления |
| **Нет ПДО** | ДЗ без даты отправления (сумма счёта) |
| **Не выставлено** | ДО без счёта и без даты оплаты клиента; исключены ИП и ООО «Взлет» |
        """
    )
