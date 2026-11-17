import io
import re
import zipfile

import pandas as pd
import plotly.express as px
import streamlit as st

st.set_page_config(page_title="Финансовый дашборд ДЗ / ДО", page_icon="📊", layout="wide")

st.title("📊 Финансовый дашборд ДЗ / ДО")
st.caption(
    "Все финансовые показатели считаются по данным из файла «дз» "
    "(сумма выставленных — из столбца «Сумма по счету»). "
    "Файл «до» используется только для расчёта «Не выставлено»."
)


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
    return f"{value:,.2f} ₽".replace(",", " ").replace(".", ",")


def build_dz(raw):
    """Все финансовые показатели — только из ДЗ. Сумма выставленных = «Сумма по счету»."""
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
        raise ValueError(
            "В файле ДЗ не найдены обязательные столбцы «Заявка.Контрагент» и/или «Счет на оплату»."
        )
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
    """Не выставлено — только из ДО: заявки без номера счёта."""
    c_client = find_col(raw_do, "Клиент")
    c_request = find_col(raw_do, "Заявка")
    c_invoice = find_col(raw_do, "Номер счета", "Номер счёта")
    c_amount = find_col(raw_do, "Ставка заказчика")
    c_load = find_col(raw_do, "Дата загрузки")
    c_date = find_col(raw_do, "Дата")
    c_manager = find_col(raw_do, "Клиентский бухгалтер", "Менеджер")

    if c_client is None or c_amount is None:
        raise ValueError("В файле ДО не найдены обязательные столбцы «Клиент» и/или «Ставка заказчика».")

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
            "Контрагент",
            "Оборот",
            "Выставлено",
            "Оплачено",
            "Просрочено",
            "Подходит к оплате",
            "Не отправлено",
            "Не выставлено",
        ]
    ]
    summary = summary.sort_values("Оборот", ascending=False).reset_index(drop=True)
    return summary


with st.sidebar:
    st.header("1. Загрузка данных")
    dz_file = st.file_uploader("Файл ДЗ (.xlsx)", type=["xlsx"], key="dz")
    do_file = st.file_uploader("Файл ДО (.xlsx)", type=["xlsx"], key="do")
    st.caption("Загрузите актуальные выгрузки «дз.xlsx» и «до.xlsx».")

if not dz_file or not do_file:
    st.info("Загрузите оба исходных файла — «дз.xlsx» и «до.xlsx», чтобы построить отчёт.")
    st.markdown(
        """
        **Логика расчёта (как в «Отчет по ДЗ НОВЫЙ»):**
        - **Выставлено / Оплачено / Просрочено / Подходит к оплате / Не отправлено** — только из файла ДЗ.
          Сумма счетов берётся из столбца **«Сумма по счету»**.
        - **Не выставлено** — из файла ДО (заявки без номера счёта, сумма = «Ставка заказчика»).
        - **Оборот** = Выставлено + Не выставлено.
        - Остаток выставленных счетов делится на три категории:
          1. **Не отправлено** — нет даты отправления;
          2. **Просрочено** — есть дата отправления и дни просрочки > 0;
          3. **Подходит к оплате** — есть дата отправления и дни просрочки ≤ 0.
        """
    )
    st.stop()

try:
    dz_raw = read_source(dz_file, header_row=4)
    do_raw = read_source(do_file, header_row=4)
    dz = build_dz(dz_raw)
    unissued = build_unissued(do_raw)
    summary = build_summary(dz, unissued)
except Exception as exc:
    st.error(f"Не удалось обработать файлы: {exc}")
    st.stop()

st.sidebar.divider()
st.sidebar.header("2. Фильтры")

clients = sorted(summary["Контрагент"].dropna().unique().tolist())
selected_clients = st.sidebar.multiselect("Контрагент", clients, default=[])

payers = sorted(
    [x for x in dz["Плательщик"].dropna().unique().tolist() if x and str(x).lower() not in ("nan", "none")]
)
selected_payers = st.sidebar.multiselect("Плательщик (ДЗ)", payers, default=[])

valid_inv = dz["Дата счета"].dropna()
if not valid_inv.empty:
    min_d, max_d = valid_inv.min().date(), valid_inv.max().date()
    invoice_range = st.sidebar.date_input(
        "Дата счёта (ДЗ)",
        value=(min_d, max_d),
        min_value=min_d,
        max_value=max_d,
        key="invoice_date",
    )
else:
    invoice_range = None
    st.sidebar.caption("Дата счёта: в данных нет дат")

valid_due = dz["Дата оплаты по договору"].dropna()
if not valid_due.empty:
    min_d, max_d = valid_due.min().date(), valid_due.max().date()
    due_range = st.sidebar.date_input(
        "Дата оплаты по договору (ДЗ)",
        value=(min_d, max_d),
        min_value=min_d,
        max_value=max_d,
        key="due_date",
    )
else:
    due_range = None
    st.sidebar.caption("Дата оплаты по договору: в данных нет дат")

dz_f = dz.copy()
unissued_f = unissued.copy()

if selected_clients:
    dz_f = dz_f[dz_f["Контрагент"].isin(selected_clients)]
    unissued_f = unissued_f[unissued_f["Контрагент"].isin(selected_clients)]

if selected_payers:
    dz_f = dz_f[dz_f["Плательщик"].isin(selected_payers)]


def apply_date_range(frame, column, date_range):
    if date_range and len(date_range) == 2 and column in frame.columns:
        start = pd.Timestamp(date_range[0])
        end = pd.Timestamp(date_range[1]) + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
        return frame[frame[column].between(start, end)]
    return frame


dz_f = apply_date_range(dz_f, "Дата счета", invoice_range)
dz_f = apply_date_range(dz_f, "Дата оплаты по договору", due_range)

summary_f = build_summary(dz_f, unissued_f)

total_oborot = summary_f["Оборот"].sum()
total_issued = summary_f["Выставлено"].sum()
total_paid = summary_f["Оплачено"].sum()
total_overdue = summary_f["Просрочено"].sum()
total_approaching = summary_f["Подходит к оплате"].sum()
total_not_sent = summary_f["Не отправлено"].sum()
total_not_issued = summary_f["Не выставлено"].sum()

c1, c2, c3, c4 = st.columns(4)
c1.metric("Оборот", rub(total_oborot))
c2.metric("Выставлено (Сумма по счету)", rub(total_issued))
c3.metric("Оплачено", rub(total_paid))
c4.metric("Не выставлено (из ДО)", rub(total_not_issued))

c5, c6, c7 = st.columns(3)
c5.metric("Просрочено", rub(total_overdue))
c6.metric("Подходит к оплате", rub(total_approaching))
c7.metric("Не отправлено", rub(total_not_sent))

st.caption(
    f"Строк ДЗ после фильтрации: {len(dz_f):,} · "
    f"Невыставленных заявок (ДО): {len(unissued_f):,}".replace(",", " ")
)

tab1, tab2, tab3, tab4 = st.tabs(
    ["📋 Сводка по контрагентам", "📈 Аналитика", "🔎 Детализация ДЗ", "⬇️ Выгрузка"]
)

with tab1:
    st.subheader("Сводка в формате «ОБЩАЯ ДЗ»")
    st.dataframe(
        summary_f.style.format({
            "Оборот": "{:,.2f}",
            "Выставлено": "{:,.2f}",
            "Оплачено": "{:,.2f}",
            "Просрочено": "{:,.2f}",
            "Подходит к оплате": "{:,.2f}",
            "Не отправлено": "{:,.2f}",
            "Не выставлено": "{:,.2f}",
        }),
        use_container_width=True,
        hide_index=True,
        height=520,
    )

with tab2:
    left, right = st.columns(2)
    with left:
        status = pd.DataFrame({
            "Категория": ["Оплачено", "Просрочено", "Подходит к оплате", "Не отправлено", "Не выставлено"],
            "Сумма": [total_paid, total_overdue, total_approaching, total_not_sent, total_not_issued],
        })
        status = status[status["Сумма"] > 0]
        if not status.empty:
            fig = px.pie(
                status,
                names="Категория",
                values="Сумма",
                hole=0.5,
                title="Структура: оплачено и остатки",
            )
            st.plotly_chart(fig, use_container_width=True)

    with right:
        bar_df = pd.DataFrame({
            "Показатель": [
                "Выставлено", "Оплачено", "Просрочено",
                "Подходит к оплате", "Не отправлено", "Не выставлено",
            ],
            "Сумма": [
                total_issued, total_paid, total_overdue,
                total_approaching, total_not_sent, total_not_issued,
            ],
        })
        fig = px.bar(
            bar_df, x="Показатель", y="Сумма",
            title="Ключевые показатели", labels={"Сумма": "Сумма, ₽"},
        )
        st.plotly_chart(fig, use_container_width=True)

    monthly = dz_f.dropna(subset=["Дата счета"]).copy()
    if not monthly.empty:
        monthly["Месяц"] = monthly["Дата счета"].dt.to_period("M").dt.to_timestamp()
        monthly = monthly.groupby("Месяц", as_index=False).agg(
            Выставлено=("Сумма счета", "sum"),
            Оплачено=("Оплачено", "sum"),
            Просрочено=("Просрочено", "sum"),
        )
        fig = px.line(
            monthly,
            x="Месяц",
            y=["Выставлено", "Оплачено", "Просрочено"],
            markers=True,
            title="Динамика выставленных / оплаченных / просроченных (по дате счёта, ДЗ)",
            labels={"value": "Сумма, ₽", "variable": "Показатель"},
        )
        st.plotly_chart(fig, use_container_width=True)

    top = summary_f.nlargest(15, "Оборот")
    if not top.empty:
        fig = px.bar(
            top.sort_values("Оборот"),
            x="Оборот",
            y="Контрагент",
            orientation="h",
            title="Топ-15 контрагентов по обороту",
            labels={"Оборот": "Оборот, ₽"},
        )
        st.plotly_chart(fig, use_container_width=True)

with tab3:
    st.subheader("Детализация по счетам (только ДЗ)")
    search = st.text_input("Поиск по контрагенту, счёту или заявке")
    view = dz_f.copy()
    if search:
        mask = (
            view["Контрагент"].astype(str).str.contains(search, case=False, na=False)
            | view["Номер счета"].astype(str).str.contains(search, case=False, na=False)
            | view["Заявка"].astype(str).str.contains(search, case=False, na=False)
        )
        view = view[mask]

    cols = [
        "Контрагент", "Плательщик", "Заявка", "Номер счета", "Дата счета",
        "Дата оплаты по договору", "Просрочка, дни", "Сумма счета", "Оплачено",
        "Остаток", "Просрочено", "Подходит к оплате", "Не отправлено", "Дата отправления",
    ]
    st.dataframe(
        view[cols].sort_values(["Дата счета", "Контрагент"], ascending=[False, True]),
        use_container_width=True,
        hide_index=True,
        height=480,
    )

    st.subheader("Невыставленные заявки (из ДО)")
    search2 = st.text_input("Поиск по контрагенту / заявке (невыставленные)", key="search_unissued")
    view_u = unissued_f.copy()
    if search2:
        mask = (
            view_u["Контрагент"].astype(str).str.contains(search2, case=False, na=False)
            | view_u["Заявка"].astype(str).str.contains(search2, case=False, na=False)
        )
        view_u = view_u[mask]
    st.dataframe(
        view_u.sort_values("Сумма", ascending=False),
        use_container_width=True,
        hide_index=True,
        height=360,
    )

with tab4:
    st.subheader("Выгрузка")
    csv_summary = summary_f.to_csv(index=False, sep=";", decimal=",", encoding="utf-8-sig").encode("utf-8-sig")
    st.download_button(
        "Скачать сводку по контрагентам (CSV)",
        data=csv_summary,
        file_name="сводка_ДЗ.csv",
        mime="text/csv",
    )

    excel_buffer = io.BytesIO()
    with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
        summary_f.to_excel(writer, index=False, sheet_name="ОБЩАЯ ДЗ")
        dz_f.to_excel(writer, index=False, sheet_name="Детализация ДЗ")
        unissued_f.to_excel(writer, index=False, sheet_name="Не выставлено (ДО)")
    st.download_button(
        "Скачать Excel (сводка + детализация + невыставленные)",
        data=excel_buffer.getvalue(),
        file_name="Отчет_ДЗ_ДО.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

with st.expander("Пояснения к расчётам"):
    st.markdown(
        """
**Источник сумм**
- **Выставлено** = сумма столбца **«Сумма по счету»** из файла ДЗ (не «Сумма по заявке»).
- **Оплачено** = сумма столбца «Сумма оплаченная» из файла ДЗ.
- **Остаток** = max(0, Выставлено − Оплачено) по каждой строке.

**Разбиение остатка (взаимоисключающие категории)**
1. **Не отправлено** — остаток по строкам, где **Дата отправления** пустая.
2. **Просрочено** — остаток по строкам, где дата отправления заполнена **и** «Количество дней просрочки счета» > 0.
3. **Подходит к оплате** — остаток по строкам, где дата отправления заполнена **и** дни просрочки ≤ 0.

**Не выставлено**
- Берётся **только** из файла ДО: заявки, у которых **Номер счета** пустой.
- Сумма = «Ставка заказчика».

**Оборот** = Выставлено + Не выставлено.

Фильтры по дате счёта и дате оплаты по договору применяются только к данным ДЗ.
        """
    )
