import time
from dataclasses import dataclass
from urllib.parse import urljoin

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup


@dataclass
class AppConfig:
    portal_base_url: str = "https://cniseng.kbro.com.tw/portal"
    login_page: str = "login.php"
    index_page: str = "index.php"
    captcha_path: str = "securimage_show.php"
    node_chart_path: str = "access/cmts/cmts_if_node_chart.php"
    cm_check_path: str = "qos/node/cmts_cm_check.php"
    company_no: str = "330"
    timeout_sec: int = 60
    node_chart_timeout_sec: int = 60
    cm_check_timeout_sec: int = 60
    retry_count: int = 2
    verify_ssl: bool = True


class PortalSession:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.session = requests.Session()
        self._apply_default_headers()

    def _apply_default_headers(self):
        base = self.cfg.portal_base_url.rstrip("/")
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/146.0.0.0 Safari/537.36"
            ),
            "Referer": urljoin(base + "/", self.cfg.index_page),
        })

    def _url(self, path: str) -> str:
        return urljoin(self.cfg.portal_base_url.rstrip("/") + "/", path.lstrip("/"))

    def open_login_page(self):
        return self.session.get(
            self._url(self.cfg.login_page),
            timeout=self.cfg.timeout_sec,
            verify=self.cfg.verify_ssl,
        )

    def fetch_captcha(self, force_new: bool = True) -> bytes:
        self.open_login_page()
        sid = str(time.time()).replace(".", "") if force_new else "static"
        r = self.session.get(
            self._url(f"{self.cfg.captcha_path}?sid={sid}"),
            timeout=self.cfg.timeout_sec,
            verify=self.cfg.verify_ssl,
        )
        r.raise_for_status()
        return r.content

    def login(self, account: str, passwd: str, captcha: str) -> dict:
        login_page_resp = self.open_login_page()
        payload = {
            "account": account.strip(),
            "passwd": passwd.strip(),
            "captcha": captcha.strip(),
        }
        r = self.session.post(
            self._url(self.cfg.login_page),
            data=payload,
            timeout=self.cfg.timeout_sec,
            verify=self.cfg.verify_ssl,
            allow_redirects=True,
        )
        text_lower = r.text.lower()
        history_urls = [h.url for h in r.history]
        final_url = r.url
        cookies = self.session.cookies.get_dict()

        success_hints = [
            "logout" in text_lower,
            "登出" in r.text,
            self.cfg.index_page in final_url,
            any(self.cfg.index_page in u for u in history_urls),
            "login failed" not in text_lower,
        ]
        ok = any(success_hints[:4]) and not ("驗證碼錯誤" in r.text or "登入失敗" in r.text)

        return {
            "ok": ok,
            "status_code": r.status_code,
            "final_url": final_url,
            "history_urls": history_urls,
            "cookies": cookies,
            "response_snippet": r.text[:800],
            "login_page_status": getattr(login_page_resp, "status_code", None),
        }

    def fetch_node_chart(self, node: str, company_no: str) -> str:
        r = self.session.get(
            self._url(self.cfg.node_chart_path),
            params={"companyno": company_no, "node": node},
            timeout=self.cfg.node_chart_timeout_sec,
            verify=self.cfg.verify_ssl,
        )
        r.raise_for_status()
        return r.text

    def fetch_cm_check(self, company_no: str, cmts_id: str, ifindex: str) -> str:
        r = self.session.post(
            self._url(self.cfg.cm_check_path),
            data={
                "item": "cmts_cm_check",
                "companyno": company_no,
                "cmts_id": cmts_id,
                "ifindex": ifindex,
            },
            timeout=self.cfg.cm_check_timeout_sec,
            verify=self.cfg.verify_ssl,
        )
        r.raise_for_status()
        return r.text


class NodeParser:
    @staticmethod
    def parse_chart_interfaces(html: str) -> list:
        soup = BeautifulSoup(html, "html.parser")
        import re

        btns = soup.select('input[id^="cm_check_"]')
        res = []
        for b in btns:
            m = re.search(
                r"cmts_cm_check\('([^']+)','([^']+)','([^']+)','([^']+)'\)",
                b.get("onclick", ""),
            )
            if m:
                res.append({
                    "companyno": m.group(1),
                    "cmts_id": m.group(2),
                    "ifindex": m.group(3),
                })
        return res

    @staticmethod
    def parse_cm_check_table(html: str):
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table")
        if not table:
            return ["內容"], []
        trs = table.find_all("tr")
        headers = [c.get_text(strip=True) for c in trs[0].find_all(["th", "td"])]
        rows = [[c.get_text(strip=True) for c in r.find_all(["td", "th"])] for r in trs[1:]]
        return headers, rows


def init_state():
    defaults = {
        "portal": PortalSession(AppConfig()),
        "logged_in": False,
        "login_debug": None,
        "captcha_img": None,
        "captcha_loaded_at": None,
        "query_running": False,
        "query_done": False,
        "node_last_query": "",
        "df_result_raw": None,
        "df_result_view": None,
        "result_col": None,
        "show_mode": "全部",
        "last_error": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def load_captcha(force_refresh: bool = False):
    if force_refresh or st.session_state.captcha_img is None:
        st.session_state.captcha_img = st.session_state.portal.fetch_captcha(force_new=True)
        st.session_state.captcha_loaded_at = time.strftime("%Y-%m-%d %H:%M:%S")


def find_result_column(df: pd.DataFrame):
    candidates = ["Result", "result", "RESULT", "狀態", "状态", "Status", "STATUS"]
    for col in candidates:
        if col in df.columns:
            return col
    for col in df.columns:
        values = df[col].astype(str).str.upper().str.strip()
        if values.isin(["ONLINE", "OFFLINE"]).any():
            return col
    return None


def normalize_result(value) -> str:
    return str(value).strip().upper()


def hide_unwanted_columns(df: pd.DataFrame) -> pd.DataFrame:
    drop_keywords = ["開機時間", "原因"]
    cols_to_drop = [col for col in df.columns if any(keyword in str(col) for keyword in drop_keywords)]
    if cols_to_drop:
        return df.drop(columns=cols_to_drop)
    return df


def apply_filter(df: pd.DataFrame, result_col: str, show_mode: str) -> pd.DataFrame:
    if df is None or df.empty or not result_col or show_mode == "全部":
        return df.copy() if df is not None else pd.DataFrame()

    values = df[result_col].astype(str).str.strip().str.upper()
    if show_mode == "ONLINE":
        return df[values == "ONLINE"].copy()
    if show_mode == "OFFLINE/其他":
        return df[values != "ONLINE"].copy()
    return df.copy()


def style_dataframe(df: pd.DataFrame, result_col: str):
    if result_col and result_col in df.columns:
        def color_result_cell(value):
            text = normalize_result(value)
            if text == "ONLINE":
                return "background-color: #d8f3dc; color: #137333; font-weight: bold;"
            return "background-color: #f8d7da; color: #b00020; font-weight: bold;"

        return df.style.map(color_result_cell, subset=[result_col])
    return df


def get_stats(df: pd.DataFrame, result_col: str):
    if df is None or df.empty:
        return 0, 0, 0, 0.0
    total = len(df)
    if not result_col or result_col not in df.columns:
        return total, 0, total, 0.0
    values = df[result_col].astype(str).str.strip().str.upper()
    online = int((values == "ONLINE").sum())
    offline = total - online
    rate = round((online / total) * 100, 2) if total else 0.0
    return total, online, offline, rate


def save_query_result(df: pd.DataFrame, result_col: str, node_id: str):
    st.session_state.df_result_raw = df.copy()
    st.session_state.result_col = result_col
    st.session_state.node_last_query = node_id
    st.session_state.query_done = True
    refresh_view_only()


def refresh_view_only():
    raw = st.session_state.df_result_raw
    result_col = st.session_state.result_col
    show_mode = st.session_state.show_mode
    if raw is None:
        st.session_state.df_result_view = None
        return
    st.session_state.df_result_view = apply_filter(raw, result_col, show_mode)


@st.dialog("查測中")
def show_progress_dialog(message: str):
    st.write(message)


def query_node(node_id: str):
    st.session_state.query_running = True
    st.session_state.last_error = None
    try:
        progress = st.progress(0, text="準備查詢 Node...")
        chart_html = st.session_state.portal.fetch_node_chart(node_id, "330")
        progress.progress(15, text="已取得 Node 介面資訊...")

        ifs = NodeParser.parse_chart_interfaces(chart_html)
        if not ifs:
            st.session_state.df_result_raw = None
            st.session_state.df_result_view = None
            st.session_state.result_col = None
            st.warning("查無介面資料，可能是 Node 不存在、尚未登入成功，或 Portal 回傳格式不同。")
            st.code(chart_html[:1500])
            return

        all_rows = []
        headers = []
        total_ifs = len(ifs)
        for idx, item in enumerate(ifs, start=1):
            h, r = NodeParser.parse_cm_check_table(
                st.session_state.portal.fetch_cm_check("330", item["cmts_id"], item["ifindex"])
            )
            headers = h
            all_rows.extend(r)
            pct = 15 + int((idx / total_ifs) * 80)
            progress.progress(min(pct, 95), text=f"查詢介面 {idx}/{total_ifs}...")

        progress.progress(100, text="查測完成")
        time.sleep(0.2)
        progress.empty()

        if not all_rows:
            st.session_state.df_result_raw = None
            st.session_state.df_result_view = None
            st.session_state.result_col = None
            st.warning("有抓到介面，但沒有查測資料。")
            return

        df = pd.DataFrame(all_rows, columns=headers)
        df = hide_unwanted_columns(df)
        result_col = find_result_column(df)
        save_query_result(df, result_col, node_id)

    except Exception as e:
        st.session_state.last_error = str(e)
        st.error(f"查測失敗：{e}")
    finally:
        st.session_state.query_running = False


def render_metric_cards():
    raw = st.session_state.df_result_raw
    result_col = st.session_state.result_col
    total, online, offline, rate = get_stats(raw, result_col)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("總台數", total)
    c2.metric("ONLINE", online)
    c3.metric("OFFLINE/其他", offline)
    c4.metric("上線率", f"{rate:.2f}%")


def render_filter_toolbar():
    st.markdown("### ⚔️ 快速篩選")
    c1, c2, c3, c4 = st.columns([1, 1, 1, 2])
    if c1.button("全部", use_container_width=True):
        st.session_state.show_mode = "全部"
        refresh_view_only()
    if c2.button("ONLINE", use_container_width=True):
        st.session_state.show_mode = "ONLINE"
        refresh_view_only()
    if c3.button("OFFLINE/其他", use_container_width=True):
        st.session_state.show_mode = "OFFLINE/其他"
        refresh_view_only()
    with c4:
        st.caption(f"目前模式：{st.session_state.show_mode}")


def render_result_area():
    if not st.session_state.query_done or st.session_state.df_result_raw is None:
        return

    st.subheader(f"📊 {st.session_state.node_last_query} 查測清單")
    if st.session_state.result_col:
        st.caption(f"狀態欄位：{st.session_state.result_col}")
    else:
        st.warning("找不到 ONLINE / OFFLINE 狀態欄位，將直接顯示原始資料。")

    render_metric_cards()
    render_filter_toolbar()

    view_df = st.session_state.df_result_view
    if view_df is None or view_df.empty:
        st.warning("目前篩選條件下沒有資料。")
        return

    styled = style_dataframe(view_df, st.session_state.result_col)
    st.dataframe(styled, use_container_width=True, height=620)


def clear_login_and_data():
    st.session_state.logged_in = False
    st.session_state.login_debug = None
    st.session_state.portal = PortalSession(AppConfig())
    st.session_state.df_result_raw = None
    st.session_state.df_result_view = None
    st.session_state.result_col = None
    st.session_state.node_last_query = ""
    st.session_state.query_done = False
    st.session_state.show_mode = "全部"
    load_captcha(force_refresh=True)


def main():
    st.set_page_config(page_title="FN總表工程戰鬥版", layout="wide")
    init_state()

    st.title("🌐 FN總表一鍵工程 - 戰鬥版 GUI")
    st.caption("登入一次、查一次、切換 ONLINE / OFFLINE 不用再重查。")

    with st.sidebar:
        st.header("🔑 Portal 登入")
        acc = st.text_input("帳號", key="login_account")
        pwd = st.text_input("密碼", type="password", key="login_password")

        c1, c2 = st.columns([1, 1])
        with c1:
            if st.button("刷新驗證碼", use_container_width=True):
                load_captcha(force_refresh=True)
        with c2:
            if st.button("清除登入", use_container_width=True):
                clear_login_and_data()
                st.rerun()

        try:
            load_captcha(force_refresh=False)
            st.image(st.session_state.captcha_img)
            st.caption(f"驗證碼載入時間：{st.session_state.captcha_loaded_at or '-'}")
        except Exception as e:
            st.error(f"連線 Portal 失敗：{e}")

        captcha_code = st.text_input("驗證碼", key="captcha_code")

        if st.button("登入", use_container_width=True):
            if not acc.strip() or not pwd.strip() or not captcha_code.strip():
                st.warning("請完整輸入帳號、密碼、驗證碼")
            else:
                with st.spinner("登入中..."):
                    result = st.session_state.portal.login(acc, pwd, captcha_code)
                    st.session_state.login_debug = result
                    if result["ok"]:
                        st.session_state.logged_in = True
                        st.success("登入成功")
                    else:
                        st.session_state.logged_in = False
                        st.error("登入失敗，請確認帳密與驗證碼是否對應同一張圖片")
                        load_captcha(force_refresh=True)

        if st.session_state.login_debug:
            with st.expander("登入偵錯資訊"):
                st.json(st.session_state.login_debug)

    if not st.session_state.logged_in:
        st.info("💡 請先在左側輸入帳號密碼登入系統。")
        return

    top1, top2, top3 = st.columns([2, 1, 1])
    with top1:
        node_id = st.text_input(
            "請輸入 Node (例如: DJ01)",
            key="node_id",
            value=st.session_state.node_last_query,
        ).upper().strip()
    with top2:
        st.write("")
        if st.button("🔍 開始查測", use_container_width=True, type="primary"):
            if not node_id:
                st.warning("請先輸入 Node")
            else:
                query_node(node_id)
    with top3:
        st.write("")
        if st.button("🧹 清空結果", use_container_width=True):
            st.session_state.df_result_raw = None
            st.session_state.df_result_view = None
            st.session_state.result_col = None
            st.session_state.node_last_query = ""
            st.session_state.query_done = False
            st.session_state.show_mode = "全部"
            st.rerun()

    if st.session_state.last_error:
        st.error(st.session_state.last_error)

    render_result_area()


if __name__ == "__main__":
    main()
