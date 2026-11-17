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
# Настройки публикации (GitHub)
# В Streamlit Cloud: Settings → Secrets
# Локально: файл .streamlit/secrets.toml
# =============================================================================
# Пример secrets.toml:
#
# admin_password = "ваш_секретный_пароль"
#
# [github]
# token = "ghp_xxxxxxxx"          # Personal Access Token с правом repo
# repo = "логин/dashboard-dz-do"  # владелец/репозиторий
# path = "data/published.json"    # путь к файлу снимка в репозитории
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
# Работа со снимком (publish / load)
# -----------------------------------------------------------------------------
def load_published_snapshot():
    """Загружает последний опубликованный снимок из GitHub."""
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
        content_b64 = r.json()["content"]
        import base64
        raw = base64.b64decode(content_b64).decode("utf-8")
        return json.loads(raw)
    except Exception as e:
        st.warning(f"Не удалось загрузить опубликованные данные: {e}")
        return None


def publish_snapshot(snapshot: dict) -> tuple[bool, str]:
    """Сохраняет снимок в GitHub (create or update)."""
    if not PUBLISH_ENABLED:
        return False, "Публикация не настроена (нет token/repo в secrets)."

    import base64

    url = f"https://api.github.com/repos/{GITHUB['repo']}/contents/{GITHUB['path']}"
    headers = {
        "Authorization": f"Bearer {GITHUB['token']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # Получаем sha текущего файла (если есть) — нужен для update
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
        r = requests.put(url, headers=headers, json=payload, timeout=60)
        if r.status_code in (200, 201):
            return True, "Снимок успешно опубликован."
        return False, f"Ошибка GitHub API ({r.status_code}): {r.text[:300]}"
    except Exception as e:
        return False, str(e)


def snapshot_from_data(summary_f: pd.DataFrame, metrics: dict) -> dict:
    """Упаковывает сводку и KPI в JSON-снимок."""
    records = summary_f.copy()
    for col in records.columns:
        if records[col].dtype == "float64":
            records[col] = records[col].round(2)
    return {
        "published_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "metrics": {k: round(float(v), 2) for k, v in metrics.items()},
        "summary": records.to_dict(orient="records"),
        "rows_count": len(records),
    }


def summary_from_snapshot(snap: dict) -> tuple[pd.DataFrame, dict]:
    metrics = snap.get("metrics", {})
    summary = pd.DataFrame(snap.get("summary", []))
    return summary, metrics


# -----------------------------------------------------------------------------
# Утилиты Excel
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
    data = uploaded_file.getvalue()
    return pd.read_excel(normalize_xlsx(data), header=header_row, engine="openpyxl")


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
# Расчёт ДЗ / ДО
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


def render_dashboard(summary_f: pd.DataFrame, metrics: dict, source_label: str):
    """Общая отрисовка KPI + вкладок для опубликованных и свежих данных."""
    st.caption(source_label)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Оборот", rub(metrics.get("Оборот", 0)))
    c2.metric("Выставлено (Сумма по счету)", rub(metrics.get("Выставлено", 0)))
    c3.metric("Оплачено", rub(metrics.get("Оплачено", 0)))
    c4.metric("Не выставлено (из ДО)", rub(metrics.get("Не выставлено", 0)))

    c5, c6, c7 = st.columns(3)
    c5.metric("Просрочено", rub(metrics.get("Просрочено", 0)))
    c6.metric("Подходит к оплате", rub(metrics.get("Подходит к оплате", 0)))
    c7.metric("Не отправлено", rub(metrics.get("Не отправлено", 0)))

    tab1, tab2, tab3 = st.tabs(["📋 Сводка по контрагентам", "📈 Аналитика", "⬇️ Выгрузка"])

    with tab1:
        st.subheader("Сводка в формате «ОБЩАЯ ДЗ»")
        if summary_f is not None and not summary_f.empty:
            fmt = {
                c: "{:,.2f}" for c in summary_f.columns if c != "Контрагент"
            }
            st.dataframe(
                summary_f.style.format(fmt),
                use_container_width=True,
                hide_index=True,
                height=520,
            )
        else:
            st.info("Нет данных для отображения.")

    with tab2:
        left, right = st.columns(2)
        with left:
            status = pd.DataFrame({
                "Категория": [
                    "Оплачено", "Просрочено", "Подходит к оплате",
                    "Не отправлено", "Не выставлено",
                ],
                "Сумма": [
                    metrics.get("Оплачено", 0),
                    metrics.get("Просрочено", 0),
                    metrics.get("Подходит к оплате", 0),
                    metrics.get("Не отправлено", 0),
                    metrics.get("Не выставлено", 0),
                ],
            })
            status = status[status["Сумма"] > 0]
            if not status.empty:
                fig = px.pie(
                    status, names="Категория", values="Сумма",
                    hole=0.5, title="Структура: оплачено и остатки",
                )
                st.plotly_chart(fig, use_container_width=True)
        with right:
            bar_df = pd.DataFrame({
                "Показатель": [
                    "Выставлено", "Оплачено", "Просрочено",
                    "Подходит к оплате", "Не отправлено", "Не выставлено",
                ],
                "Сумма": [
                    metrics.get("Выставлено", 0),
                    metrics.get("Оплачено", 0),
                    metrics.get("Просрочено", 0),
                    metrics.get("Подходит к оплате", 0),
                    metrics.get("Не отправлено", 0),
                    metrics.get("Не выставлено", 0),
                ],
            })
            fig = px.bar(
                bar_df, x="Показатель", y="Сумма",
                title="Ключевые показатели", labels={"Сумма": "Сумма, ₽"},
            )
            st.plotly_chart(fig, use_container_width=True)

        if summary_f is not None and not summary_f.empty:
            top = summary_f.nlargest(15, "Оборот")
            fig = px.bar(
                top.sort_values("Оборот"),
                x="Оборот", y="Контрагент", orientation="h",
                title="Топ-15 контрагентов по обороту",
                labels={"Оборот": "Оборот, ₽"},
            )
            st.plotly_chart(fig, use_container_width=True)

    with tab3:
        if summary_f is not None and not summary_f.empty:
            csv = summary_f.to_csv(index=False, sep=";", decimal=",", encoding="utf-8-sig").encode("utf-8-sig")
            st.download_button(
                "Скачать сводку (CSV)",
                data=csv,
                file_name="сводка_ДЗ.csv",
                mime="text/csv",
            )
            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                summary_f.to_excel(writer, index=False, sheet_name="ОБЩАЯ ДЗ")
            st.download_button(
                "Скачать сводку (Excel)",
                data=buf.getvalue(),
                file_name="Отчет_ОБЩАЯ_ДЗ.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        else:
            st.info("Нет данных для выгрузки.")


# =============================================================================
# UI
# =============================================================================
st.title("📊 Финансовый дашборд ДЗ / ДО")

# --- Режим администратора (загрузка и публикация) ---
with st.sidebar:
    st.header("Режим")
    is_admin = False
    if ADMIN_PASSWORD:
        pwd = st.text_input("Пароль администратора", type="password", key="admin_pwd")
        is_admin = pwd == ADMIN_PASSWORD
        if pwd and not is_admin:
            st.error("Неверный пароль")
    else:
        st.caption("Пароль не задан в secrets — режим загрузки открыт локально.")
        is_admin = st.checkbox("Режим загрузки и публикации", value=False)

    if is_admin:
        st.success("Режим администратора")
        st.divider()
        st.header("Загрузка данных")
        dz_file = st.file_uploader("Файл ДЗ (.xlsx)", type=["xlsx"], key="dz")
        do_file = st.file_uploader("Файл ДО (.xlsx)", type=["xlsx"], key="do")
        st.caption("После расчёта нажмите «Опубликовать» — начальник увидит эти цифры по ссылке.")
    else:
        dz_file = None
        do_file = None

# --- Публичный просмотр: всегда пробуем загрузить снимок ---
published = load_published_snapshot()

# --- Админ загрузил файлы → считаем «живой» отчёт ---
live_summary = None
live_metrics = None
live_dz = None
live_unissued = None

if is_admin and dz_file and do_file:
    try:
        dz_raw = read_source(dz_file, header_row=4)
        do_raw = read_source(do_file, header_row=4)
        live_dz = build_dz(dz_raw)
        live_unissued = build_unissued(do_raw)
        live_summary = build_summary(live_dz, live_unissued)
        live_metrics = {
            "Оборот": float(live_summary["Оборот"].sum()),
            "Выставлено": float(live_summary["Выставлено"].sum()),
            "Оплачено": float(live_summary["Оплачено"].sum()),
            "Просрочено": float(live_summary["Просрочено"].sum()),
            "Подходит к оплате": float(live_summary["Подходит к оплате"].sum()),
            "Не отправлено": float(live_summary["Не отправлено"].sum()),
            "Не выставлено": float(live_summary["Не выставлено"].sum()),
        }
    except Exception as exc:
        st.error(f"Не удалось обработать файлы: {exc}")

# --- Кнопка публикации (только админ + есть расчёт) ---
if is_admin and live_summary is not None:
    st.sidebar.divider()
    if not PUBLISH_ENABLED:
        st.sidebar.warning(
            "Публикация не настроена. Добавьте в secrets:\n"
            "admin_password, github.token, github.repo"
        )
    else:
        if st.sidebar.button("📢 Опубликовать для руководства", type="primary"):
            snap = snapshot_from_data(live_summary, live_metrics)
            ok, msg = publish_snapshot(snap)
            if ok:
                st.sidebar.success(msg)
                st.balloons()
                # обновляем кэш снимка
                published = snap
            else:
                st.sidebar.error(msg)

# --- Что показывать ---
if live_summary is not None:
    # Админ видит свежий расчёт
    render_dashboard(
        live_summary,
        live_metrics,
        source_label=(
            f"Черновик (ещё не опубликован) · строк ДЗ: {len(live_dz):,} · "
            f"невыставленных: {len(live_unissued):,}".replace(",", " ")
        ),
    )
    # Детализация только в режиме админа
    with st.expander("🔎 Детализация счетов (ДЗ) и невыставленные (ДО)"):
        st.subheader("Счета (ДЗ)")
        st.dataframe(live_dz, use_container_width=True, hide_index=True, height=300)
        st.subheader("Не выставлено (ДО)")
        st.dataframe(live_unissued, use_container_width=True, hide_index=True, height=300)

elif published is not None:
    summary_f, metrics = summary_from_snapshot(published)
    when = published.get("published_at", "—")
    render_dashboard(
        summary_f,
        metrics,
        source_label=f"Опубликованные данные · {when}",
    )
else:
    st.info(
        "Пока нет опубликованных данных.\n\n"
        "Администратор: введите пароль в боковой панели, загрузите «дз.xlsx» и «до.xlsx», "
        "затем нажмите **«Опубликовать для руководства»**."
    )
    if not PUBLISH_ENABLED and not is_admin:
        st.caption(
            "Для постоянной ссылки настройте Streamlit secrets "
            "(admin_password + github.token + github.repo) и задеплойте приложение."
        )

with st.expander("Пояснения к расчётам"):
    st.markdown(
        """
**Источник сумм**
- **Выставлено** = «Сумма по счету» из файла ДЗ
- **Оплачено** = «Сумма оплаченная» из файла ДЗ
- **Не выставлено** = «Ставка заказчика» из ДО по заявкам без номера счёта
- **Оборот** = Выставлено + Не выставлено

**Остаток** (Выставлено − Оплачено) делится так:
1. **Не отправлено** — нет даты отправления
2. **Просрочено** — дата есть и дни просрочки > 0
3. **Подходит к оплате** — дата есть и дни просрочки ≤ 0

**Как видит руководство**  
Администратор загружает файлы и нажимает «Опубликовать».  
Снимок сохраняется в GitHub. По той же ссылке руководство сразу видит последние опубликованные цифры — без загрузки файлов.
        """
    )
