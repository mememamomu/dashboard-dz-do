import base64
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

# =============================================================================
# Secrets (Streamlit Cloud → Settings → Secrets)
#
# admin_password = "пароль"
#
# [github]
# token = "ghp_..."
# repo = "логин/dashbord-dz-do"
# path = "data/published.json"
# branch = "main"
# =============================================================================


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
            "path": gh.get("path", "data/published.json"),
            "branch": gh.get("branch", "main"),
        }
    except Exception:
        return {"token": "", "repo": "", "path": "data/published.json", "branch": "main"}


ADMIN_PASSWORD = get_secret("admin_password", "")
GITHUB = get_github_cfg()
PUBLISH_ENABLED = bool(GITHUB["token"] and GITHUB["repo"])


# -----------------------------------------------------------------------------
# Публикация
# -----------------------------------------------------------------------------
def load_published_snapshot():
    if not PUBLISH_ENABLED:
        return None
    url = (
        f"https://api.github.com/repos/{GITHUB['repo']}/contents/{GITHUB['path']}"
        f"?ref={GITHUB['branch']}"
    )
    headers = {
        "Authorization": f"Bearer {GITHUB['token']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        raw = base64.b64decode(r.json()["content"]).decode("utf-8")
        return json.loads(raw)
    except Exception as e:
        st.warning(f"Не удалось загрузить опубликованные данные: {e}")
        return None


def publish_snapshot(snapshot: dict) -> tuple[bool, str]:
    if not PUBLISH_ENABLED:
        return False, "Публикация не настроена (нет token/repo в secrets)."
    url = f"https://api.github.com/repos/{GITHUB['repo']}/contents/{GITHUB['path']}"
    headers = {
        "Authorization": f"Bearer {GITHUB['token']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    sha = None
    try:
        r = requests.get(url + f"?ref={GITHUB['branch']}", headers=headers, timeout=30)
        if r.status_code == 200:
            sha = r.json().get("sha")
    except Exception:
        pass
    payload = {
        "message": f"Публикация снимка ДЗ/ДО {snapshot.get('published_at', '')}",
        "content": base64.b64encode(
            json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8")
        ).decode("ascii"),
        "branch": GITHUB["branch"],
    }
    if sha:
        payload["sha"] = sha
    try:
        r = requests.put(url, headers=headers, json=payload, timeout=90)
        if r.status_code in (200, 201):
            return True, "Снимок успешно опубликован."
        return False, f"Ошибка GitHub API ({r.status_code}): {r.text[:400]}"
    except Exception as e:
        return False, str(e)


def _dt_to_str(v):
    if pd.isna(v):
        return None
    try:
        return pd.Timestamp(v).strftime("%Y-%m-%d")
    except Exception:
        return None


def snapshot_from_frames(dz: pd.DataFrame, unissued: pd.DataFrame) -> dict:
    """В снимок кладём строки ДЗ/ДО — чтобы фильтры работали у всех."""
    dz_cols = [
        "Контрагент", "Плательщик", "Заявка", "Номер счета",
        "Дата счета", "Дата оплаты по договору", "Просрочка, дни",
        "Сумма счета", "Оплачено", "Остаток",
        "Просрочено", "Подходит к оплате", "Не отправлено", "Дата отправления",
    ]
    dz_export = dz[[c for c in dz_cols if c in dz.columns]].copy()
    for c in ("Дата счета", "Дата оплаты по договору", "Дата отправления"):
        if c in dz_export.columns:
            dz_export[c] = dz_export[c].map(_dt_to_str)

    un_cols = ["Контрагент", "Заявка", "Сумма", "Дата загрузки", "Дата заявки", "Бухгалтер"]
    un_export = unissued[[c for c in un_cols if c in unissued.columns]].copy()
    for c in ("Дата загрузки", "Дата заявки"):
        if c in un_export.columns:
            un_export[c] = un_export[c].map(_dt_to_str)

    return {
        "published_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "dz": dz_export.to_dict(orient="records"),
        "unissued": un_export.to_dict(orient="records"),
    }


def frames_from_snapshot(snap: dict):
    dz = pd.DataFrame(snap.get("dz", []))
    unissued = pd.DataFrame(snap.get("unissued", []))
    for c in ("Дата счета", "Дата оплаты по договору", "Дата отправления"):
        if c in dz.columns:
            dz[c] = pd.to_datetime(dz[c], errors="coerce")
    for c in ("Дата загрузки", "Дата заявки"):
        if c in unissued.columns:
            unissued[c] = pd.to_datetime(unissued[c], errors="coerce")
    for c in ("Сумма счета", "Оплачено", "Остаток", "Просрочено", "Подходит к оплате",
              "Не отправлено", "Просрочка, дни"):
        if c in dz.columns:
            dz[c] = pd.to_numeric(dz[c], errors="coerce").fillna(0)
    if "Сумма" in unissued.columns:
        unissued["Сумма"] = pd.to_numeric(unissued["Сумма"], errors="coerce").fillna(0)
    return dz, unissued


# -----------------------------------------------------------------------------
# Excel utils
# -----------------------------------------------------------------------------
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


# -----------------------------------------------------------------------------
# Расчёты
# -----------------------------------------------------------------------------
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

    df["Остаток"] = (df["Сумма счета"] - df["Оплачено"]).clip(lower=0)
    no_sent = df["Дата отправления"].isna()
    is_late = df["Просрочка, дни"] > 0
    df["Не отправлено"] = df["Остаток"].where(no_sent, 0)
    df["Просрочено"] = df["Остаток"].where(~no_sent & is_late, 0)
    df["Подходит к оплате"] = df["Остаток"].where(~no_sent & ~is_late, 0)
    return df


def build_unissued(raw_do):
    c_client = find_col(raw_do, "Клиент")
    c_request = find_col(raw_do, "Заявка")
    c_invoice = find_col(raw_do, "Номер счета", "Номер счёта")
    c_amount = find_col(raw_do, "Ставка заказчика")
    c_load = find_col(raw_do, "Дата загрузки")
    c_date = find_col(raw_do, "Дата")
    c_manager = find_col(raw_do, "Клиентский бухгалтер", "Менеджер")

    if c_client is None or c_amount is None:
        raise ValueError("В файле ДО не найдены «Клиент» и/или «Ставка заказчика».")

    amount = number(raw_do[c_amount])
    invoice = raw_do[c_invoice].astype(str).str.strip() if c_invoice else pd.Series([""] * len(raw_do))
    no_invoice = (
        invoice.isna()
        | (invoice == "")
        | (invoice.str.lower() == "nan")
        | (invoice.str.lower() == "none")
    )
    df = pd.DataFrame({
        "Контрагент": raw_do[c_client].astype(str).str.strip(),
        "Заявка": raw_do[c_request] if c_request else "",
        "Номер счета": invoice,
        "Сумма": amount,
        "Дата загрузки": to_date(raw_do[c_load]) if c_load else pd.NaT,
        "Дата заявки": to_date(raw_do[c_date]) if c_date else pd.NaT,
        "Бухгалтер": raw_do[c_manager].astype(str).str.strip() if c_manager else "",
    })
    df = df[
        no_invoice
        & (df["Контрагент"] != "")
        & (df["Контрагент"].str.lower() != "nan")
        & (df["Сумма"] != 0)
    ].copy()
    return df


def build_summary(dz: pd.DataFrame, unissued: pd.DataFrame) -> pd.DataFrame:
    if dz is None or dz.empty:
        issued = pd.DataFrame(columns=[
            "Контрагент", "Выставлено", "Оплачено", "Просрочено",
            "Подходит к оплате", "Не отправлено",
        ])
    else:
        issued = (
            dz.groupby("Контрагент", as_index=False)
            .agg(
                Выставлено=("Сумма счета", "sum"),
                Оплачено=("Оплачено", "sum"),
                Просрочено=("Просрочено", "sum"),
                **{"Подходит к оплате": ("Подходит к оплате", "sum")},
                **{"Не отправлено": ("Не отправлено", "sum")},
            )
        )
    if unissued is None or unissued.empty:
        not_issued = pd.DataFrame(columns=["Контрагент", "Не выставлено"])
    else:
        not_issued = (
            unissued.groupby("Контрагент", as_index=False)
            .agg(**{"Не выставлено": ("Сумма", "sum")})
        )
    summary = issued.merge(not_issued, on="Контрагент", how="outer").fillna(0)
    summary["Оборот"] = summary["Выставлено"] + summary["Не выставлено"]
    summary = summary[
        [
            "Контрагент", "Оборот", "Выставлено", "Оплачено",
            "Просрочено", "Подходит к оплате", "Не отправлено", "Не выставлено",
        ]
    ]
    return summary.sort_values("Оборот", ascending=False).reset_index(drop=True)


def build_payment_plan(dz: pd.DataFrame) -> pd.DataFrame:
    if dz is None or dz.empty:
        return pd.DataFrame()
    plan = dz[(dz["Остаток"] > 0) & dz["Дата оплаты по договору"].notna()].copy()
    if plan.empty:
        return pd.DataFrame()
    iso = plan["Дата оплаты по договору"].dt.isocalendar()
    plan["Неделя"] = iso["year"].astype(str) + "-W" + iso["week"].astype(str).str.zfill(2)
    pivot = (
        plan.groupby(["Контрагент", "Неделя"], as_index=False)["Остаток"]
        .sum()
        .pivot(index="Контрагент", columns="Неделя", values="Остаток")
        .fillna(0)
    )
    week_cols = sorted(pivot.columns.tolist())
    pivot = pivot[week_cols]
    pivot["Итого"] = pivot.sum(axis=1)
    return pivot.sort_values("Итого", ascending=False)


def metrics_from_summary(summary_f: pd.DataFrame) -> dict:
    if summary_f is None or summary_f.empty:
        return {k: 0.0 for k in [
            "Оборот", "Выставлено", "Оплачено", "Просрочено",
            "Подходит к оплате", "Не отправлено", "Не выставлено",
        ]}
    return {
        "Оборот": float(summary_f["Оборот"].sum()),
        "Выставлено": float(summary_f["Выставлено"].sum()),
        "Оплачено": float(summary_f["Оплачено"].sum()),
        "Просрочено": float(summary_f["Просрочено"].sum()),
        "Подходит к оплате": float(summary_f["Подходит к оплате"].sum()),
        "Не отправлено": float(summary_f["Не отправлено"].sum()),
        "Не выставлено": float(summary_f["Не выставлено"].sum()),
    }


def apply_filters(dz, unissued, clients, payers, invoice_range):
    dz_f = dz.copy() if dz is not None else pd.DataFrame()
    un_f = unissued.copy() if unissued is not None else pd.DataFrame()

    if clients:
        if not dz_f.empty:
            dz_f = dz_f[dz_f["Контрагент"].isin(clients)]
        if not un_f.empty:
            un_f = un_f[un_f["Контрагент"].isin(clients)]
    if payers and not dz_f.empty and "Плательщик" in dz_f.columns:
        dz_f = dz_f[dz_f["Плательщик"].isin(payers)]
    if invoice_range and len(invoice_range) == 2 and not dz_f.empty and "Дата счета" in dz_f.columns:
        start = pd.Timestamp(invoice_range[0])
        end = pd.Timestamp(invoice_range[1]) + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
        dz_f = dz_f[dz_f["Дата счета"].between(start, end)]
    return dz_f, un_f


# -----------------------------------------------------------------------------
# UI helpers
# -----------------------------------------------------------------------------
def render_kpi(metrics: dict):
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Оборот", rub(metrics.get("Оборот", 0)))
    c2.metric("Выставлено (Сумма по счету)", rub(metrics.get("Выставлено", 0)))
    c3.metric("Оплачено", rub(metrics.get("Оплачено", 0)))
    c4.metric("Не выставлено (из ДО)", rub(metrics.get("Не выставлено", 0)))
    c5, c6, c7 = st.columns(3)
    c5.metric("Просрочено", rub(metrics.get("Просрочено", 0)))
    c6.metric("Подходит к оплате", rub(metrics.get("Подходит к оплате", 0)))
    c7.metric("Не отправлено", rub(metrics.get("Не отправлено", 0)))


def render_payment_plan(plan_df: pd.DataFrame):
    st.subheader("Планирование оплат по неделям")
    st.caption(
        "Остаток к оплате по ISO-неделе «Даты оплаты по договору». "
        "Пример: 24.11.2026 и 26.11.2026 → неделя 2026-W48, суммы складываются."
    )
    if plan_df is None or plan_df.empty:
        st.info("Нет строк с остатком и датой оплаты по договору.")
        return
    st.dataframe(plan_df.style.format("{:,.2f}"), use_container_width=True, height=520)
    csv = plan_df.reset_index().to_csv(index=False, sep=";", decimal=",", encoding="utf-8-sig").encode("utf-8-sig")
    st.download_button("Скачать план оплат (CSV)", data=csv,
                       file_name="план_оплат_по_неделям.csv", mime="text/csv", key="dl_plan_csv")
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        plan_df.reset_index().to_excel(writer, index=False, sheet_name="План оплат")
    st.download_button(
        "Скачать план оплат (Excel)", data=buf.getvalue(),
        file_name="план_оплат_по_неделям.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="dl_plan_xlsx",
    )


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
            fmt = {c: "{:,.2f}" for c in summary_f.columns if c != "Контрагент"}
            st.dataframe(summary_f.style.format(fmt), use_container_width=True, hide_index=True, height=520)
        else:
            st.info("Нет данных.")

    with tabs[1]:
        render_payment_plan(plan_df)

    with tabs[2]:
        left, right = st.columns(2)
        with left:
            status = pd.DataFrame({
                "Категория": ["Оплачено", "Просрочено", "Подходит к оплате", "Не отправлено", "Не выставлено"],
                "Сумма": [
                    metrics.get("Оплачено", 0), metrics.get("Просрочено", 0),
                    metrics.get("Подходит к оплате", 0), metrics.get("Не отправлено", 0),
                    metrics.get("Не выставлено", 0),
                ],
            })
            status = status[status["Сумма"] > 0]
            if not status.empty:
                fig = px.pie(status, names="Категория", values="Сумма", hole=0.5,
                             title="Структура: оплачено и остатки")
                st.plotly_chart(fig, use_container_width=True)
        with right:
            bar_df = pd.DataFrame({
                "Показатель": [
                    "Выставлено", "Оплачено", "Просрочено",
                    "Подходит к оплате", "Не отправлено", "Не выставлено",
                ],
                "Сумма": [
                    metrics.get("Выставлено", 0), metrics.get("Оплачено", 0),
                    metrics.get("Просрочено", 0), metrics.get("Подходит к оплате", 0),
                    metrics.get("Не отправлено", 0), metrics.get("Не выставлено", 0),
                ],
            })
            fig = px.bar(bar_df, x="Показатель", y="Сумма", title="Ключевые показатели",
                         labels={"Сумма": "Сумма, ₽"})
            st.plotly_chart(fig, use_container_width=True)

        if summary_f is not None and not summary_f.empty:
            top = summary_f.nlargest(15, "Оборот")
            fig = px.bar(
                top.sort_values("Оборот"), x="Оборот", y="Контрагент", orientation="h",
                title="Топ-15 контрагентов по обороту", labels={"Оборот": "Оборот, ₽"},
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
                title="Динамика по месяцам (дата счёта, ДЗ)",
                labels={"value": "Сумма, ₽", "variable": "Показатель"},
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
                "Остаток", "Просрочено", "Подходит к оплате", "Не отправлено", "Дата отправления",
            ] if c in view.columns]
            st.dataframe(
                view[cols].sort_values(
                    [c for c in ["Дата счета", "Контрагент"] if c in view.columns],
                    ascending=[False, True],
                ),
                use_container_width=True, hide_index=True, height=420,
            )
        else:
            st.info("Нет детализации ДЗ.")

        if unissued_f is not None and not unissued_f.empty:
            st.subheader("Невыставленные заявки (ДО)")
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
                    unissued_f.to_excel(writer, index=False, sheet_name="Не выставлено")
            st.download_button(
                "Скачать Excel (сводка + план + детализация)",
                data=buf.getvalue(), file_name="Отчет_ДЗ_ДО.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )


# =============================================================================
# Главный поток
# =============================================================================
st.title("📊 Финансовый дашборд ДЗ / ДО")

# --- Админ: загрузка ---
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

# Базовые данные: либо свежая загрузка админа, либо опубликованный снимок
base_dz = base_unissued = None
source_mode = None  # "live" | "published"

if is_admin and dz_file and do_file:
    try:
        base_dz = build_dz(read_source(dz_file, header_row=4))
        base_unissued = build_unissued(read_source(do_file, header_row=4))
        source_mode = "live"
    except Exception as exc:
        st.error(f"Не удалось обработать файлы: {exc}")

if base_dz is None:
    published = load_published_snapshot()
    if published is not None and published.get("dz") is not None:
        base_dz, base_unissued = frames_from_snapshot(published)
        source_mode = "published"
        st.sidebar.caption(f"Опубликовано: {published.get('published_at', '—')}")
    elif published is not None and published.get("summary"):
        # Старый формат снимка (только summary) — без полноценных фильтров по датам
        st.warning(
            "Опубликован старый формат снимка. Администратору нужно "
            "заново загрузить файлы и нажать «Опубликовать», "
            "чтобы фильтры работали у всех."
        )
        summary_old = pd.DataFrame(published.get("summary", []))
        metrics_old = published.get("metrics") or metrics_from_summary(summary_old)
        plan_old = pd.DataFrame(published.get("payment_plan", []))
        if not plan_old.empty and "Контрагент" in plan_old.columns:
            plan_old = plan_old.set_index("Контрагент")
        with st.sidebar:
            st.header("Фильтры")
            if not summary_old.empty and "Контрагент" in summary_old.columns:
                clients = sorted(summary_old["Контрагент"].dropna().unique().tolist())
                sel = st.multiselect("Контрагент", clients, default=[])
                if sel:
                    summary_old = summary_old[summary_old["Контрагент"].isin(sel)]
                    metrics_old = metrics_from_summary(summary_old)
                    if not plan_old.empty:
                        plan_old = plan_old.loc[plan_old.index.isin(sel)]
        render_dashboard(summary_old, metrics_old, plan_old, None, None,
                         source_label=f"Опубликованные данные (старый снимок) · {published.get('published_at', '—')}")
        st.stop()

if base_dz is None:
    st.info(
        "Нет данных для отображения.\n\n"
        "Администратор: пароль → загрузка «дз.xlsx» и «до.xlsx» → «Опубликовать для руководства»."
    )
    st.stop()

# --- Фильтры для ВСЕХ ---
with st.sidebar:
    st.divider()
    st.header("Фильтры")

    all_clients = set(base_dz["Контрагент"].dropna().unique().tolist()) if not base_dz.empty else set()
    if base_unissued is not None and not base_unissued.empty:
        all_clients |= set(base_unissued["Контрагент"].dropna().unique().tolist())
    clients = sorted(all_clients)
    selected_clients = st.multiselect("Контрагент", clients, default=[])

    payers = []
    if not base_dz.empty and "Плательщик" in base_dz.columns:
        payers = sorted([
            x for x in base_dz["Плательщик"].dropna().unique().tolist()
            if x and str(x).lower() not in ("nan", "none")
        ])
    selected_payers = st.multiselect("Плательщик", payers, default=[]) if payers else []

    invoice_range = None
    if not base_dz.empty and "Дата счета" in base_dz.columns:
        valid_inv = base_dz["Дата счета"].dropna()
        if not valid_inv.empty:
            min_d, max_d = valid_inv.min().date(), valid_inv.max().date()
            invoice_range = st.date_input(
                "Дата счёта",
                value=(min_d, max_d),
                min_value=min_d,
                max_value=max_d,
                key="invoice_date",
            )

dz_f, unissued_f = apply_filters(base_dz, base_unissued, selected_clients, selected_payers, invoice_range)
summary_f = build_summary(dz_f, unissued_f)
metrics = metrics_from_summary(summary_f)
plan_df = build_payment_plan(dz_f)

st.sidebar.caption(
    f"Строк ДЗ: {len(dz_f):,} · невыставленных: {len(unissued_f):,}".replace(",", " ")
)

# --- Публикация (только админ + живые файлы) ---
if is_admin and source_mode == "live":
    st.sidebar.divider()
    if not PUBLISH_ENABLED:
        st.sidebar.warning("Публикация не настроена (secrets).")
    else:
        # Публикуем полный набор БЕЗ фильтров — чтобы у всех был полный срез,
        # а фильтры каждый настраивает у себя.
        if st.sidebar.button("📢 Опубликовать для руководства", type="primary"):
            snap = snapshot_from_frames(base_dz, base_unissued)
            ok, msg = publish_snapshot(snap)
            if ok:
                st.sidebar.success(msg)
                st.balloons()
            else:
                st.sidebar.error(msg)

label = (
    "Данные из загруженных файлов (черновик)"
    if source_mode == "live"
    else f"Опубликованные данные"
)
render_dashboard(summary_f, metrics, plan_df, dz_f, unissued_f, source_label=label)

with st.expander("Пояснения"):
    st.markdown(
        """
**Фильтры** доступны и администратору, и тем, кто открыл ссылку:
- Контрагент
- Плательщик
- Дата счёта

**Планирование оплат** — остаток по ISO-неделе даты оплаты по договору
(24.11.2026 → 2026-W48; несколько платежей в одной неделе суммируются).

**Публикация** сохраняет полный срез данных. Фильтры каждый применяет у себя.
        """
    )

