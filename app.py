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


@st.cache_data(ttl=600, show_spinner="Загрузка опубликованных данных…")
def _load_published_snapshot_cached(repo: str, path: str, branch: str, token: str):
    """Кэш на 10 минут — фильтры не дергают GitHub повторно."""
    if not (token and repo):
        return None
    api_url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={branch}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    r = requests.get(api_url, headers=headers, timeout=60)
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise RuntimeError(f"GitHub API {r.status_code}: {r.text[:400]}")
    meta = r.json()
    raw_bytes = None
    if meta.get("content"):
        raw_bytes = base64.b64decode(meta["content"])
    elif meta.get("download_url"):
        r2 = requests.get(
            meta["download_url"],
            headers={"Authorization": f"Bearer {token}"},
            timeout=180,
        )
        if r2.status_code != 200:
            raise RuntimeError(f"download_url HTTP {r2.status_code}: {r2.text[:300]}")
        raw_bytes = r2.content
    elif meta.get("sha"):
        blob_url = f"https://api.github.com/repos/{repo}/git/blobs/{meta['sha']}"
        r3 = requests.get(blob_url, headers=headers, timeout=180)
        if r3.status_code == 200:
            blob = r3.json()
            if blob.get("content"):
                raw_bytes = base64.b64decode(blob["content"])
    if not raw_bytes:
        return None
    try:
        text = gzip.decompress(raw_bytes).decode("utf-8")
    except OSError:
        text = raw_bytes.decode("utf-8")
    text = text.strip()
    if not text:
        return None
    return json.loads(text)


def load_published_snapshot():
    if not PUBLISH_ENABLED:
        return None
    try:
        return _load_published_snapshot_cached(
            GITHUB["repo"], GITHUB["path"], GITHUB["branch"], GITHUB["token"]
        )
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
    # Счёт считается отправленным, если есть дата отправления ИЛИ уже есть оплата
    no_sent = df["Дата отправления"].isna() & (df["Оплачено"] <= 0)

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
    # Не выставлено: нет счёта, нет оплаты клиента, не ИП/Взлет, сумма > 2
    df["Не выставлено"] = df["Сумма"].where(no_invoice & no_client_pay & ~excluded & (df["Сумма"] > 2), 0)
    return df



def _month_label(dt):
    if pd.isna(dt):
        return None
    ts = pd.Timestamp(dt)
    return f"{ts.year}-{ts.month:02d}"


def prepare_do_full(raw_do: pd.DataFrame) -> pd.DataFrame:
    """Полная таблица ДО для разделов Невыставленные / Неотправленные."""
    c_client = find_col(raw_do, "Клиент")
    c_request = find_col(raw_do, "Заявка")
    c_invoice = find_col(raw_do, "Номер счета", "Номер счёта")
    c_amount = find_col(raw_do, "Ставка заказчика")
    c_load = find_col(raw_do, "Дата загрузки")
    c_unload = find_col(raw_do, "Дата выгрузки")
    c_paid_date = find_col(raw_do, "Дата оплаты клиента")
    c_due = find_col(raw_do, "Дата оплаты по договору")
    c_sent = find_col(raw_do, "Дата отправки", "Дата отправления")
    c_delivery = find_col(raw_do, "Дата доставки")
    c_arrival = find_col(raw_do, "Дата прихода")
    c_manager = find_col(raw_do, "Клиентский бухгалтер", "Менеджер")

    if c_client is None or c_amount is None:
        raise ValueError("В файле ДО не найдены «Клиент» и/или «Ставка заказчика».")

    amount = number(raw_do[c_amount])
    invoice = raw_do[c_invoice].astype(str).str.strip() if c_invoice else pd.Series([""] * len(raw_do))
    inv_empty = (
        invoice.isna() | (invoice == "") | (invoice.str.lower() == "nan")
        | (invoice.str.lower() == "none") | (invoice == "0")
    )

    df = pd.DataFrame({
        "Заявка": raw_do[c_request] if c_request else "",
        "Клиент": raw_do[c_client].astype(str).str.strip(),
        "Номер счета": invoice.where(~inv_empty, ""),
        "Ставка заказчика": amount,
        "Дата загрузки": to_date(raw_do[c_load]) if c_load else pd.NaT,
        "Дата выгрузки": to_date(raw_do[c_unload]) if c_unload else pd.NaT,
        "Дата оплаты клиента": to_date(raw_do[c_paid_date]) if c_paid_date else pd.NaT,
        "Дата оплаты по договору": to_date(raw_do[c_due]) if c_due else pd.NaT,
        "Дата отправки": to_date(raw_do[c_sent]) if c_sent else pd.NaT,
        "Дата доставки": to_date(raw_do[c_delivery]) if c_delivery else pd.NaT,
        "Дата прихода": to_date(raw_do[c_arrival]) if c_arrival else pd.NaT,
        "Ответственный сотрудник ДО": (
            raw_do[c_manager].astype(str).str.strip() if c_manager else ""
        ),
        "_no_invoice": inv_empty,
    })
    df = df[
        (df["Клиент"] != "")
        & (df["Клиент"].str.lower() != "nan")
    ].copy()
    df["_excluded"] = df["Клиент"].map(exclude_client_from_unissued)
    df["Месяц"] = df["Дата выгрузки"].map(_month_label)
    return df


def build_not_invoiced_section(do_full: pd.DataFrame):
    """Невыставленные: ставка > 2, нет счёта, нет оплаты клиента, не ИП/Взлет.
    Спец. переносы дат и исключения по клиентам — см. ниже.
    """
    if do_full is None or do_full.empty:
        return pd.DataFrame(), pd.DataFrame()
    mask = (
        (do_full["Ставка заказчика"] > 2)
        & do_full["_no_invoice"]
        & do_full["Дата оплаты клиента"].isna()
        & ~do_full["_excluded"]
    )
    work = do_full.loc[mask].copy()
    if work.empty:
        return pd.DataFrame(), pd.DataFrame()

    work["Дата выгрузки"] = pd.to_datetime(work["Дата выгрузки"], errors="coerce", dayfirst=True)
    work["Месяц"] = work["Дата выгрузки"].map(_month_label)

    # Перенос авг–окт → 01.11.2026 для спец. клиентов
    special_keys = (
        "опх", "газпромнефть-снабжение", "газпромнефть снабжение",
        "фактор", "транснефть-логистика", "транснефть логистика",
    )

    def _is_special(name: str) -> bool:
        s = str(name or "").lower().replace("«", '"').replace("»", '"').replace("“", '"').replace("”", '"')
        return any(k in s for k in special_keys)

    is_spec = work["Клиент"].map(_is_special)
    unload = work["Дата выгрузки"]
    in_aso = unload.dt.month.isin([8, 9, 10])
    shift = is_spec & in_aso & unload.notna()
    work.loc[shift, "Дата выгрузки"] = pd.Timestamp("2026-11-01")
    work.loc[shift, "Месяц"] = "2026-11"

    # Исключить янв–сен 2026 для РИКОС, Интернет Решения, ФК Открытие, Роксор, Элита-Центр
    def _is_exclude_client(name: str) -> bool:
        s = str(name or "").lower().replace("«", '"').replace("»", '"').replace("“", '"').replace("”", '"')
        s = s.replace("ё", "е")
        if "рикос" in s:
            return True
        if "интернет" in s and "решен" in s:
            return True
        if "роксор" in s:
            return True
        if "элита" in s and "центр" in s:
            return True
        if "открытие" in s and ("фк" in s or "банк" in s):
            return True
        return False

    is_excl = work["Клиент"].map(_is_exclude_client)
    # и по дате, и по метке месяца (надёжнее)
    unload = work["Дата выгрузки"]
    in_jan_sep_2026 = (
        ((unload.dt.year == 2026) & (unload.dt.month >= 1) & (unload.dt.month <= 9))
        | work["Месяц"].isin([f"2026-{m:02d}" for m in range(1, 10)])
    )
    drop_mask = is_excl & in_jan_sep_2026
    work = work.loc[~drop_mask].copy()

    if work.empty:
        return pd.DataFrame(), pd.DataFrame()

    detail = work[[
        "Заявка", "Клиент", "Дата загрузки", "Дата выгрузки",
        "Номер счета", "Ставка заказчика", "Ответственный сотрудник ДО", "Месяц",
    ]].copy()
    detail = detail.rename(columns={
        "Заявка": "Номер заявки",
        "Клиент": "Заказчик/клиент",
        "Дата загрузки": "Дата погрузки/загрузки",
    })

    pivot_src = work[work["Месяц"].notna()].copy()
    if pivot_src.empty:
        summary = (
            work.groupby(["Клиент", "Ответственный сотрудник ДО"], as_index=False)["Ставка заказчика"]
            .sum()
            .rename(columns={"Ставка заказчика": "Итого"})
        )
        return summary, detail

    pivot = (
        pivot_src.groupby(["Клиент", "Ответственный сотрудник ДО", "Месяц"], as_index=False)["Ставка заказчика"]
        .sum()
        .pivot_table(
            index=["Клиент", "Ответственный сотрудник ДО"],
            columns="Месяц",
            values="Ставка заказчика",
            aggfunc="sum",
            fill_value=0,
        )
    )
    months = sorted(pivot.columns.tolist())
    pivot = pivot[months]
    pivot["Итого"] = pivot[months].sum(axis=1)
    pivot = pivot.sort_values("Итого", ascending=False).reset_index()

    # Строка ИТОГО сверху — суммы по месяцам
    total = {c: pivot[c].sum() if c not in ("Клиент", "Ответственный сотрудник ДО") else "" for c in pivot.columns}
    total["Клиент"] = "ИТОГО"
    total["Ответственный сотрудник ДО"] = ""
    pivot = pd.concat([pd.DataFrame([total]), pivot], ignore_index=True)

    return pivot, detail


def build_not_sent_section(do_full: pd.DataFrame):
    """
    Неотправленные заявки (раздел):
    есть счёт и/или дата прихода оригиналов,
    но нет даты отправки, даты оплаты по договору и даты оплаты клиента;
    не ИП / Взлет.
    """
    if do_full is None or do_full.empty:
        return pd.DataFrame(), pd.DataFrame()
    has_invoice = ~do_full["_no_invoice"]
    has_arrival = do_full["Дата прихода"].notna()
    no_sent = do_full["Дата отправки"].isna()
    no_due = do_full["Дата оплаты по договору"].isna()
    no_client_pay = do_full["Дата оплаты клиента"].isna()
    mask = (
        (has_invoice | has_arrival)
        & no_sent & no_due & no_client_pay
        & ~do_full["_excluded"]
        & (do_full["Ставка заказчика"] > 2)
    )
    detail = do_full.loc[mask, [
        "Заявка", "Клиент", "Дата загрузки", "Дата выгрузки",
        "Номер счета", "Дата прихода", "Ставка заказчика",
        "Ответственный сотрудник ДО", "Месяц",
    ]].copy()
    detail = detail.rename(columns={
        "Заявка": "Номер заявки",
        "Клиент": "Заказчик/клиент",
        "Дата загрузки": "Дата погрузки/загрузки",
    })
    if detail.empty:
        return pd.DataFrame(), detail

    pivot_src = do_full.loc[mask].copy()
    pivot_src = pivot_src[pivot_src["Месяц"].notna()]
    if pivot_src.empty:
        summary = (
            do_full.loc[mask]
            .groupby(["Клиент", "Ответственный сотрудник ДО"], as_index=False)["Ставка заказчика"]
            .sum()
            .rename(columns={"Ставка заказчика": "Итого"})
        )
        return summary, detail

    pivot = (
        pivot_src.groupby(["Клиент", "Ответственный сотрудник ДО", "Месяц"], as_index=False)["Ставка заказчика"]
        .sum()
        .pivot_table(
            index=["Клиент", "Ответственный сотрудник ДО"],
            columns="Месяц",
            values="Ставка заказчика",
            aggfunc="sum",
            fill_value=0,
        )
    )
    months = sorted(pivot.columns.tolist())
    pivot = pivot[months]
    pivot["Итого"] = pivot[months].sum(axis=1)
    pivot = pivot.sort_values("Итого", ascending=False).reset_index()
    return pivot, detail


def _norm_request(v) -> str:
    s = str(v).strip()
    if s.lower() in ("nan", "none", ""):
        return ""
    if s.endswith(".0"):
        s = s[:-2]
    # убрать пробелы и ведущие нули аккуратно
    s = s.replace(" ", "")
    return s


def build_deferral_analytics(dz: pd.DataFrame, do_full: pd.DataFrame):
    """
    Заявка со счётом: Дата оплаты по договору (ДЗ) − Дата выгрузки (ДО).
    Факт: Дата оплаты клиента (ДО) − Дата выгрузки (ДО).
    Возвращает (сводка по клиентам, series фактических сроков для гистограммы).
    """
    empty = pd.DataFrame(), pd.Series(dtype=float)
    if dz is None or dz.empty or do_full is None or do_full.empty:
        return empty
    if "Заявка" not in dz.columns or "Дата оплаты по договору" not in dz.columns:
        return empty
    if "Заявка" not in do_full.columns or "Дата выгрузки" not in do_full.columns:
        return empty

    dz_part = dz.copy()
    dz_part["_req"] = dz_part["Заявка"].map(_norm_request)
    dz_part = dz_part[dz_part["_req"] != ""]
    # только строки ДЗ с датой оплаты по договору (есть счёт/обязательство)
    dz_part = dz_part[dz_part["Дата оплаты по договору"].notna()]
    dz_due = (
        dz_part.groupby("_req", as_index=False)
        .agg(**{"Дата оплаты по договору": ("Дата оплаты по договору", "min")})
    )

    do_part = do_full.copy()
    do_part["_req"] = do_part["Заявка"].map(_norm_request)
    do_part = do_part[do_part["_req"] != ""]
    # заявки, по которым есть счёт в ДО или есть в ДЗ
    has_invoice = ~do_part["_no_invoice"] if "_no_invoice" in do_part.columns else True
    do_part = do_part.loc[has_invoice | do_part["_req"].isin(set(dz_due["_req"]))].copy()

    do_use = do_part[[
        "_req", "Клиент", "Дата выгрузки", "Дата оплаты клиента"
    ]].drop_duplicates(subset=["_req"], keep="first")

    merged = do_use.merge(dz_due, on="_req", how="inner")
    merged = merged[merged["Дата выгрузки"].notna()]

    merged["Отсрочка по договору, дни"] = (
        pd.to_datetime(merged["Дата оплаты по договору"]) - pd.to_datetime(merged["Дата выгрузки"])
    ).dt.days
    merged["Фактический срок оплаты, дни"] = (
        pd.to_datetime(merged["Дата оплаты клиента"]) - pd.to_datetime(merged["Дата выгрузки"])
    ).dt.days

    fact_series = merged["Фактический срок оплаты, дни"].dropna()
    fact_series = fact_series[fact_series.between(-30, 365)]  # отсечь явный мусор

    if "Клиент" not in merged.columns:
        return empty

    agg = (
        merged.groupby("Клиент", as_index=False)
        .agg(
            **{
                "Средняя отсрочка по договору, дни": ("Отсрочка по договору, дни", "mean"),
                "Фактический срок оплаты, дни": ("Фактический срок оплаты, дни", "mean"),
                "Заявок": ("_req", "count"),
            }
        )
    )
    for c in ("Средняя отсрочка по договору, дни", "Фактический срок оплаты, дни"):
        agg[c] = pd.to_numeric(agg[c], errors="coerce").round(1)
    agg["Отклонение, дни"] = (
        agg["Фактический срок оплаты, дни"] - agg["Средняя отсрочка по договору, дни"]
    ).round(1)
    agg = agg.sort_values("Отклонение, дни", ascending=False).reset_index(drop=True)
    return agg, fact_series



def snapshot_from_frames(dz: pd.DataFrame, unissued: pd.DataFrame, do_full: pd.DataFrame | None = None) -> dict:
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

    do_payload = []
    if do_full is not None and not do_full.empty:
        do_cols = [
            "Заявка", "Клиент", "Номер счета", "Ставка заказчика",
            "Дата загрузки", "Дата выгрузки", "Дата оплаты клиента",
            "Дата оплаты по договору", "Дата отправки", "Дата доставки",
            "Дата прихода", "Ответственный сотрудник ДО", "_no_invoice",
        ]
        dfo = do_full[[c for c in do_cols if c in do_full.columns]].copy()
        for c in ("Дата загрузки", "Дата выгрузки", "Дата оплаты клиента",
                  "Дата оплаты по договору", "Дата отправки", "Дата доставки", "Дата прихода"):
            if c in dfo.columns:
                dfo[c] = dfo[c].map(_dt_str)
        if "Ставка заказчика" in dfo.columns:
            dfo["Ставка заказчика"] = pd.to_numeric(dfo["Ставка заказчика"], errors="coerce").fillna(0).round(2)
        do_payload = json.loads(dfo.to_json(orient="records", force_ascii=False))

    return {
        "version": 5,
        "published_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "dz": json.loads(dz_out.to_json(orient="records", force_ascii=False)),
        "unissued": json.loads(un_out.to_json(orient="records", force_ascii=False)),
        "do_full": do_payload,
    }


def frames_from_snapshot(snap: dict):
    dz = pd.DataFrame(snap.get("dz", []))
    unissued = pd.DataFrame(snap.get("unissued", []))
    do_full = pd.DataFrame(snap.get("do_full", []))
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
    for c in ("Дата загрузки", "Дата выгрузки", "Дата оплаты клиента",
              "Дата оплаты по договору", "Дата отправки", "Дата доставки", "Дата прихода"):
        if c in do_full.columns:
            do_full[c] = pd.to_datetime(do_full[c], errors="coerce", dayfirst=True)
    if "Ставка заказчика" in do_full.columns:
        do_full["Ставка заказчика"] = pd.to_numeric(do_full["Ставка заказчика"], errors="coerce").fillna(0)
    if not do_full.empty:
        if "_no_invoice" not in do_full.columns and "Номер счета" in do_full.columns:
            inv = do_full["Номер счета"].astype(str).str.strip()
            do_full["_no_invoice"] = inv.isna() | (inv == "") | (inv.str.lower() == "nan") | (inv == "0")
        if "_excluded" not in do_full.columns and "Клиент" in do_full.columns:
            do_full["_excluded"] = do_full["Клиент"].map(exclude_client_from_unissued)
        if "Месяц" not in do_full.columns and "Дата выгрузки" in do_full.columns:
            do_full["Месяц"] = do_full["Дата выгрузки"].map(_month_label)
        if "Ответственный сотрудник ДО" not in do_full.columns:
            do_full["Ответственный сотрудник ДО"] = ""
    dz = ensure_dz_categories(dz)
    unissued = ensure_unissued_categories(unissued)
    return dz, unissued, do_full



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
    """4 000 000,00 ₽"""
    try:
        return f"{float(value):,.2f} ₽".replace(",", " ").replace(".", ",")
    except Exception:
        return "—"


def fmt_num(value):
    """4 000 000,00 без валюты — для таблиц."""
    try:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return ""
        return f"{float(value):,.2f}".replace(",", " ").replace(".", ",")
    except Exception:
        return ""


def format_money_df(df: pd.DataFrame, exclude=None) -> pd.DataFrame:
    """Копия таблицы с суммами в виде 4 000 000,00 (строки). Быстрее Styler."""
    if df is None or df.empty:
        return df
    exclude = set(exclude or [])
    out = df.copy()
    for c in out.columns:
        if c in exclude:
            continue
        if pd.api.types.is_numeric_dtype(out[c]):
            out[c] = out[c].map(fmt_num)
    return out


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
    if dz is None:
        dz = pd.DataFrame()
    elif "Просрочено" not in dz.columns:
        dz = ensure_dz_categories(dz)
    if unissued is None:
        unissued = pd.DataFrame()
    elif "Не выставлено" not in unissued.columns:
        unissued = ensure_unissued_categories(unissued)

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
    """
    План оплат:
    - недели — только непросроченные остатки (дни просрочки >= 0) с датой оплаты по договору;
    - «Просрочка» — просроченные суммы;
    - «Общая задолженность (непросроченная)» — сразу после контрагента.
    """
    if dz is None or dz.empty:
        return pd.DataFrame()
    if "Просрочено" not in dz.columns or "Остаток" not in dz.columns:
        dz = ensure_dz_categories(dz)

    overdue = dz.groupby("Контрагент", as_index=True)["Просрочено"].sum().rename("Просрочка")

    # Непросроченный остаток с ПДО (для недель и общей задолженности)
    non_overdue = dz[
        (dz["Остаток"] > 0)
        & dz["Дата оплаты по договору"].notna()
        & (dz["Просрочка, дни"] >= 0)
    ].copy()

    if non_overdue.empty and overdue.sum() == 0:
        return pd.DataFrame()

    if non_overdue.empty:
        result = overdue.to_frame()
        result = result[result["Просрочка"] > 0].sort_values("Просрочка", ascending=False)
        result.insert(0, "Общая задолженность (непросроченная)", 0.0)
        total = result.sum(axis=0).to_frame().T
        total.index = ["ИТОГО"]
        return pd.concat([total, result])

    non_overdue["Неделя"] = non_overdue["Дата оплаты по договору"].map(isocal_week_label)
    pivot = (
        non_overdue.groupby(["Контрагент", "Неделя"], as_index=False)["Остаток"].sum()
        .pivot(index="Контрагент", columns="Неделя", values="Остаток")
        .fillna(0)
    )
    week_cols = sorted([c for c in pivot.columns if c is not None])
    pivot = pivot[week_cols]

    # Непросроченная задолженность = сумма по неделям
    pivot.insert(0, "Общая задолженность (непросроченная)", pivot[week_cols].sum(axis=1) if week_cols else 0.0)

    pivot = pivot.join(overdue, how="left")
    pivot["Просрочка"] = pivot["Просрочка"].fillna(0)

    # Клиенты только с просрочкой
    only_overdue = overdue[~overdue.index.isin(pivot.index)]
    only_overdue = only_overdue[only_overdue > 0]
    if not only_overdue.empty:
        extra = pd.DataFrame(0.0, index=only_overdue.index, columns=["Общая задолженность (непросроченная)"] + week_cols)
        extra["Просрочка"] = only_overdue
        pivot = pd.concat([pivot, extra])

    # Порядок: Общая задолженность → Просрочка → недели
    ordered = ["Общая задолженность (непросроченная)", "Просрочка"] + week_cols
    ordered = [c for c in ordered if c in pivot.columns]
    pivot = pivot[ordered].sort_values("Общая задолженность (непросроченная)", ascending=False)

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
        ("Выставлено", "Выставлено"),
        ("Оплачено", "Оплачено"),
        ("Неоплачено", "Неоплачено"),
        ("Просрочено", "Просрочено"),
        ("Есть в ДЗ", "Есть в ДЗ"),
        ("Подходит к оплате", "Подходит к оплате"),
        ("Не отправлено", "Не отправлено"),
        ("Нет ПДО", "Нет ПДО"),
        ("Не выставлено", "Не выставлено"),
    ]
    for row in (order[0:4], order[4:8], order[8:10]):
        cols = st.columns(len(row))
        for col, (key, label) in zip(cols, row):
            col.metric(label, rub(metrics.get(key, 0)))


@st.fragment
def render_plan_tab(plan_df):
    """Смена фильтров плана не пересчитывает весь дашборд."""
    st.subheader("Планирование оплат по неделям")
    st.caption(
        "«Общая задолженность (непросроченная)» — остатки с ПДО, ещё не просроченные. "
        "«Просрочка» — просроченные суммы. Фильтры ниже не перезагружают остальные разделы."
    )
    if plan_df is None or plan_df.empty:
        st.info("Нет данных для плана оплат.")
        return

    debt_col = "Общая задолженность (непросроченная)"
    client_index = [i for i in plan_df.index if str(i) != "ИТОГО"]
    week_cols = [
        c for c in plan_df.columns
        if c not in (debt_col, "Просрочка", "Итого") and str(c).startswith("20")
    ]

    f1, f2 = st.columns(2)
    with f1:
        sel_clients = st.multiselect("Контрагент", options=client_index, default=[], key="plan_clients")
    with f2:
        sel_weeks = st.multiselect("Неделя", options=week_cols, default=[], key="plan_weeks")

    view = plan_df
    if sel_clients:
        body = plan_df.loc[[i for i in plan_df.index if i in sel_clients]]
        if not body.empty:
            total = body.sum(axis=0).to_frame().T
            total.index = ["ИТОГО"]
            view = pd.concat([total, body])
        else:
            view = plan_df.iloc[0:0]

    # Столбцы: задолженность, просрочка, выбранные недели
    fixed = [c for c in [debt_col, "Просрочка"] if c in view.columns]
    if sel_weeks:
        show_weeks = [w for w in sel_weeks if w in view.columns]
    else:
        show_weeks = week_cols

    view = view.copy()
    # Пересчёт непросроченной задолженности по видимым неделям
    if debt_col in view.columns:
        if show_weeks:
            view[debt_col] = view[show_weeks].sum(axis=1)
        else:
            view[debt_col] = 0.0

    cols_show = [c for c in (fixed + show_weeks) if c in view.columns]
    view = view[cols_show]

    body_rows = [i for i in view.index if str(i) != "ИТОГО"]
    body = view.loc[body_rows] if body_rows else view.iloc[0:0]
    # Сумма к оплате = непросроченная по выбранным неделям
    if debt_col in body.columns and not body.empty:
        extra_sum = float(pd.to_numeric(body[debt_col], errors="coerce").fillna(0).sum())
    else:
        week_cols_show = [c for c in body.columns if str(c).startswith("20")]
        extra_sum = float(body[week_cols_show].to_numpy().sum()) if (not body.empty and week_cols_show) else 0.0

    st.metric("Сумма к оплате по выбранным параметрам (непросроченная)", rub(extra_sum))
    st.caption(
        ("контрагентов: " + str(len(sel_clients)) if sel_clients else "все контрагенты")
        + " · "
        + ("недели: " + ", ".join(sel_weeks) if sel_weeks else "все недели")
    )
    st.dataframe(format_money_df(view), use_container_width=True, height=520)



def render_dashboard(summary_f, metrics, plan_df, dz_f, unissued_f, not_inv_sum, not_inv_det, not_sent_sum, not_sent_det, deferral_df, fact_deferral_series=None, source_label=""):
    if source_label:
        st.caption(source_label)
    render_kpi(metrics)

    tabs = st.tabs([
        "📋 Сводка по контрагентам",
        "📅 Планирование оплат",
        "📄 Невыставленные счета",
        "📬 Неотправленные заявки",
        "📈 Аналитика",
        "🔎 Детализация",
        "⬇️ Выгрузка",
    ])

    with tabs[0]:
        st.subheader("Сводка в формате «ОБЩАЯ ДЗ»")
        if summary_f is not None and not summary_f.empty:
            show = summary_f.drop(columns=["Задолженность"], errors="ignore")
            st.dataframe(
                format_money_df(show, exclude={"Контрагент"}),
                use_container_width=True, hide_index=True, height=520,
            )
        else:
            st.info("Нет данных.")

    with tabs[1]:
        render_plan_tab(plan_df)

    with tabs[2]:
        st.subheader("Невыставленные счета")
        st.caption(
            "Ставка заказчика > 2, нет счёта, нет даты оплаты клиента; исключены ИП и ООО «Взлет». "
            "Месяцы — по дате выгрузки."
        )
        if not_inv_sum is None or not_inv_sum.empty:
            st.info("Нет невыставленных заявок по заданным условиям.")
        else:
            st.dataframe(format_money_df(not_inv_sum, exclude={"Клиент", "Ответственный сотрудник ДО"}),
                         use_container_width=True, hide_index=True, height=360)
        st.markdown("**Детализация невыставленных заявок**")
        if not_inv_det is None or not_inv_det.empty:
            st.info("Нет строк детализации.")
        else:
            st.dataframe(format_money_df(not_inv_det, exclude={
                "Номер заявки", "Заказчик/клиент", "Дата погрузки/загрузки", "Дата выгрузки",
                "Номер счета", "Ответственный сотрудник ДО", "Месяц",
            }), use_container_width=True, hide_index=True, height=360)

    with tabs[3]:
        st.subheader("Неотправленные заявки")
        st.caption(
            "Есть счёт и/или дата прихода, но нет даты отправки, даты оплаты по договору и даты оплаты клиента; "
            "исключены ИП и ООО «Взлет». Месяцы — по дате выгрузки."
        )
        if not_sent_sum is None or not_sent_sum.empty:
            st.info("Нет неотправленных заявок по заданным условиям.")
        else:
            st.dataframe(format_money_df(not_sent_sum, exclude={"Клиент", "Ответственный сотрудник ДО"}),
                         use_container_width=True, hide_index=True, height=360)
        st.markdown("**Детализация неотправленных заявок**")
        if not_sent_det is None or not_sent_det.empty:
            st.info("Нет строк детализации.")
        else:
            st.dataframe(format_money_df(not_sent_det, exclude={
                "Номер заявки", "Заказчик/клиент", "Дата погрузки/загрузки", "Дата выгрузки",
                "Номер счета", "Дата прихода", "Ответственный сотрудник ДО", "Месяц",
            }), use_container_width=True, hide_index=True, height=360)

    with tabs[4]:
        st.subheader("Топ контрагентов по обороту")
        if summary_f is not None and not summary_f.empty and "Оборот" in summary_f.columns:
            tmp = summary_f.copy()
            tmp["Оборот"] = pd.to_numeric(tmp["Оборот"], errors="coerce").fillna(0)
            n = min(15, len(tmp))
            if n > 0:
                top = tmp.nlargest(n, "Оборот")
                fig = px.bar(
                    top.sort_values("Оборот"),
                    x="Оборот", y="Контрагент", orientation="h",
                    title="Топ-15 по обороту",
                    labels={"Оборот": "Оборот, ₽"},
                )
                st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("Нет данных для топа по обороту.")

        st.subheader("Динамика по месяцам (суммы)")
        if dz_f is not None and not dz_f.empty and "Дата счета" in dz_f.columns and dz_f["Дата счета"].notna().any():
            monthly = dz_f.dropna(subset=["Дата счета"]).copy()
            monthly["Месяц"] = monthly["Дата счета"].dt.to_period("M").dt.to_timestamp()
            monthly_sum = monthly.groupby("Месяц", as_index=False).agg(
                Выставлено=("Сумма счета", "sum"),
                Оплачено=("Оплачено", "sum"),
                Просрочено=("Просрочено", "sum"),
            )
            fig = px.line(
                monthly_sum, x="Месяц",
                y=["Выставлено", "Оплачено", "Просрочено"],
                markers=True,
                title="Суммы: выставлено / оплачено / просрочено",
                labels={"value": "Сумма, ₽", "variable": "Показатель"},
            )
            st.plotly_chart(fig, use_container_width=True)

        else:
            st.info("Нет дат счетов для помесячной динамики.")

        st.subheader("Сравнение отсрочки: по договору vs факт")
        st.caption(
            "Отсрочка по договору = Дата оплаты по договору (ДЗ, по номеру заявки) − Дата выгрузки (ДО). "
            "Факт = Дата оплаты клиента (ДО) − Дата выгрузки (ДО)."
        )
        if deferral_df is None or deferral_df.empty:
            st.info("Недостаточно данных для расчёта отсрочки.")
        else:
            st.dataframe(deferral_df, use_container_width=True, hide_index=True, height=360)

        st.subheader("Распределение фактических сроков оплаты")
        st.caption("По какой отсрочке (в днях) приходится наибольшая масса оплат.")
        if fact_deferral_series is None or len(fact_deferral_series) == 0:
            st.info("Нет фактических сроков оплаты для диаграммы.")
        else:
            hist_df = pd.DataFrame({"Фактический срок, дни": fact_deferral_series})
            # Корзины по 15 дней
            bins = list(range(0, int(max(hist_df["Фактический срок, дни"].max(), 1)) + 16, 15))
            if len(bins) < 2:
                bins = [0, 15, 30, 45, 60, 90, 120, 180, 365]
            hist_df["Корзина"] = pd.cut(hist_df["Фактический срок, дни"], bins=bins, right=False)
            dist = hist_df.groupby("Корзина", observed=False).size().reset_index(name="Количество")
            dist["Корзина"] = dist["Корзина"].astype(str)
            fig3 = px.bar(
                dist, x="Корзина", y="Количество",
                title="Масса фактических сроков оплаты (дни)",
                labels={"Корзина": "Интервал дней", "Количество": "Число заявок"},
            )
            st.plotly_chart(fig3, use_container_width=True)

    with tabs[5]:
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
            money_cols = {
                "Сумма счета", "Оплачено", "Остаток", "Просрочено", "Есть в ДЗ",
                "Подходит к оплате", "Не отправлено", "Нет ПДО", "Сумма",
            }
            view_show = view[cols].copy()
            for c in view_show.columns:
                if c in money_cols and c in view_show.columns:
                    view_show[c] = pd.to_numeric(view_show[c], errors="coerce").map(fmt_num)
            st.dataframe(view_show, use_container_width=True, hide_index=True, height=420)
        else:
            st.info("Нет детализации ДЗ.")

        if unissued_f is not None and not unissued_f.empty:
            st.subheader("Заявки из ДО")
            u = unissued_f.sort_values("Сумма", ascending=False)
            st.dataframe(
                format_money_df(u, exclude={"Контрагент", "Заявка", "Номер счета", "Бухгалтер",
                                           "Дата загрузки", "Дата заявки", "Дата оплаты клиента"}),
                use_container_width=True, hide_index=True, height=320,
            )

    with tabs[6]:
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
base_do_full = pd.DataFrame()
source_mode = None

# session_state: данные грузим один раз, фильтры их не трогают
need_reload = False
if is_admin and dz_file and do_file:
    file_key = f"{getattr(dz_file,'name','')}:{getattr(dz_file,'size',0)}:{getattr(do_file,'name','')}:{getattr(do_file,'size',0)}"
    if st.session_state.get("_upload_key") != file_key:
        need_reload = True
        st.session_state["_upload_key"] = file_key
        st.session_state.pop("base_dz", None)

if (not need_reload and st.session_state.get("base_dz") is not None):
    base_dz = st.session_state["base_dz"]
    base_unissued = st.session_state["base_unissued"]
    base_do_full = st.session_state.get("base_do_full", pd.DataFrame())
    source_mode = st.session_state.get("source_mode", "published")
    if source_mode == "published" and st.session_state.get("published_at"):
        st.sidebar.caption(f"Опубликовано: {st.session_state['published_at']}")
elif is_admin and dz_file and do_file:
    try:
        with st.spinner("Обработка Excel…"):
            raw_do = read_source(do_file, header_row=4)
            base_dz = build_dz(read_source(dz_file, header_row=4))
            base_unissued = build_unissued(raw_do)
            base_do_full = prepare_do_full(raw_do)
        source_mode = "live"
        st.session_state["base_dz"] = base_dz
        st.session_state["base_unissued"] = base_unissued
        st.session_state["base_do_full"] = base_do_full
        st.session_state["source_mode"] = source_mode
        st.session_state["published_at"] = None
    except Exception as exc:
        st.error(f"Не удалось обработать файлы: {exc}")
else:
    snap = load_published_snapshot()
    if snap is not None and snap.get("dz") is not None:
        data_key = str(snap.get("published_at", ""))
        if st.session_state.get("_data_key") == data_key and st.session_state.get("base_dz") is not None:
            base_dz = st.session_state["base_dz"]
            base_unissued = st.session_state["base_unissued"]
            base_do_full = st.session_state.get("base_do_full", pd.DataFrame())
        else:
            with st.spinner("Подготовка данных…"):
                base_dz, base_unissued, base_do_full = frames_from_snapshot(snap)
            st.session_state["base_dz"] = base_dz
            st.session_state["base_unissued"] = base_unissued
            st.session_state["base_do_full"] = base_do_full
            st.session_state["_data_key"] = data_key
        source_mode = "published"
        st.session_state["source_mode"] = source_mode
        st.session_state["published_at"] = snap.get("published_at", "—")
        st.sidebar.caption(f"Опубликовано: {st.session_state['published_at']}")
    elif snap is not None:
        st.warning("Снимок старого формата. Загрузите файлы и опубликуйте заново.")
        st.stop()

if base_dz is None:
    st.info("Нет данных. Администратор: пароль → дз.xlsx + до.xlsx → «Опубликовать».")
    st.stop()

if "base_do_full" not in st.session_state:
    st.session_state["base_do_full"] = pd.DataFrame()
base_do_full = st.session_state.get("base_do_full", pd.DataFrame())

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

plan_df = build_payment_plan(dz_f) if dz_f is not None and not dz_f.empty else pd.DataFrame()

# Разделы Невыставленные / Неотправленные / аналитика отсрочки
_do = st.session_state.get("base_do_full", pd.DataFrame())
if _do is None or (isinstance(_do, pd.DataFrame) and _do.empty):
    not_inv_sum = not_inv_det = not_sent_sum = not_sent_det = pd.DataFrame()
    deferral_df = pd.DataFrame()
    fact_deferral_series = pd.Series(dtype=float)
else:
    # фильтр по выбранным контрагентам (если есть)
    do_f = _do
    if selected_clients and "Клиент" in do_f.columns:
        do_f = do_f[do_f["Клиент"].isin(selected_clients)]
    not_inv_sum, not_inv_det = build_not_invoiced_section(do_f)
    not_sent_sum, not_sent_det = build_not_sent_section(do_f)
    deferral_df, fact_deferral_series = build_deferral_analytics(dz_f, do_f)

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
                snap = snapshot_from_frames(base_dz, base_unissued, st.session_state.get("base_do_full"))
                ok, msg = publish_snapshot(snap)
            if ok:
                st.success(msg)
                st.balloons()
                _load_published_snapshot_cached.clear()
                for _k in ("base_dz", "base_unissued", "base_do_full", "_data_key"): st.session_state.pop(_k, None)
            else:
                st.error(msg)

label = "Данные из загруженных файлов" if source_mode == "live" else "Опубликованные данные"
render_dashboard(summary_f, metrics, plan_df, dz_f, unissued_f, not_inv_sum, not_inv_det, not_sent_sum, not_sent_det, deferral_df, fact_deferral_series, source_label=label)

with st.expander("Пояснения к расчётам"):
    st.markdown(
        f"""
**Порядок:** Оборот → Выставлено → Оплачено → Неоплачено → Просрочено → Есть в ДЗ →  
Подходит к оплате → Не отправлено → Нет ПДО → Не выставлено

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
