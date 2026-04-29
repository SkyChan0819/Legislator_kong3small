import requests
import re
import time
import json
import io
import pdfplumber
from bs4 import BeautifulSoup
from datetime import datetime
import csv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class LYApiClient:
    """立法院資料串接客戶端 - 結合 Open Data API (ID:148) 與 PPG/IVOD 網頁爬蟲"""
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "identity",
            "Connection": "close",
        })
        retry = Retry(
            total=4,
            connect=4,
            read=3,
            status=3,
            backoff_factor=1,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.verify = False
        self.api_base = "https://data.ly.gov.tw/odw/openDatasetJson.action"

    def _get(self, url, **kwargs):
        kwargs.setdefault("timeout", (10, 60))
        kwargs.setdefault("verify", False)
        return self.session.get(url, **kwargs)

    # ================================================================
    # 1. Open Data API (ID: 148) - 委員發言片段相關影片資訊
    # ================================================================
    
    def fetch_speeches_by_api(self, meeting_dates, bill_keywords, max_pages=50):
        """
        使用 Open Data API (ID: 148) 抓取委員發言片段資訊。
        
        策略：
        - 抓取所有資料 (selectTerm=all)，逐頁掃描
        - 用 meetingDate 篩選指定日期的會議
        - 用 meetingContent 篩選包含法案關鍵字的議程
        
        Args:
            meeting_dates: list of str, 例如 ['2024-05-24', '2024-05-28']
            bill_keywords: list of str, 例如 ['職權行使法', '刑法']
            max_pages: 最大頁數
        Returns:
            list of dict (API 回傳的 jsonList 中符合條件的項目)
        """
        matched = []
        page = 1
        
        while page <= max_pages:
            try:
                params = {
                    "id": "148",
                    "selectTerm": "all",
                    "page": page
                }
                response = self._get(self.api_base, params=params, timeout=(10, 30))
                response.raise_for_status()
                data = response.json()
                
                items = data.get("jsonList", [])
                if not items:
                    break
                
                for item in items:
                    item_date = item.get("meetingDate", "")
                    item_content = item.get("meetingContent", "") or ""
                    
                    # 條件1: 日期需在指定日期列表中
                    date_match = (not meeting_dates) or (item_date in meeting_dates)
                    
                    # 條件2: 議程內容需包含法案關鍵字
                    keyword_match = False
                    if not bill_keywords:
                        keyword_match = True
                    else:
                        for kw in bill_keywords:
                            if kw in item_content:
                                keyword_match = True
                                break
                    
                    if date_match and keyword_match:
                        matched.append(item)
                
                page += 1
                time.sleep(0.3)
                
            except Exception as e:
                print(f"API Error on page {page}: {e}")
                break
        
        return matched

    # ================================================================
    # 2. PPG 議案頁面解析
    # ================================================================
    
    def parse_bill_page(self, ppg_url):
        """
        解析 PPG 議案頁面，取得：
        - 法案名稱 (title)
        - IVOD 影片連結 (ivod_links) 及其對應的會議日期
        - 相關會議日期列表 (meeting_dates)
        """
        try:
            response = self._get(ppg_url, timeout=(10, 60))
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 取得法案名稱
            title = ""
            if soup.title:
                title = soup.title.text.strip()
            
            # 取得 IVOD 連結
            ivod_links = []
            for a in soup.find_all('a'):
                href = a.get('href', '')
                if 'ivod.ly.gov.tw/Demand/NewsClip' in href:
                    ivod_links.append(href)
            ivod_links = list(dict.fromkeys(ivod_links))
            
            # 從 IVOD 連結中擷取日期 (格式: Querydate=20240524)
            meeting_dates = []
            ivod_committees = []
            for link in ivod_links:
                date_match = re.search(r'Querydate=(\d{8})', link)
                if date_match:
                    raw = date_match.group(1)
                    formatted = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
                    if formatted not in meeting_dates:
                        meeting_dates.append(formatted)
                comm_match = re.search(r'Committeename=([^&]+)', link)
                if comm_match:
                    import urllib.parse
                    committee_name = urllib.parse.unquote(comm_match.group(1))
                    if committee_name and committee_name not in ivod_committees:
                        ivod_committees.append(committee_name)
            
            # 從審議進度擷取會次資訊 (格式: 11-02-13)
            gazette_queries = []
            texts = soup.find_all(string=re.compile(r'\d{2}-\d{2}-\d{2}'))
            for text in texts:
                match = re.search(r'(\d{2})-(\d{2})-(\d{2})', text)
                if match:
                    term, session, times = match.groups()
                    query = {"term": int(term), "sessionPeriod": int(session), "sessionTimes": int(times)}
                    if query not in gazette_queries:
                        gazette_queries.append(query)
            
            return {
                "url": ppg_url,
                "title": title,
                "ivod_links": ivod_links,
                "meeting_dates": meeting_dates,
                "ivod_committees": ivod_committees,
                "gazette_queries": gazette_queries,
            }
        except Exception as e:
            return {
                "url": ppg_url,
                "title": f"解析失敗: {e}",
                "ivod_links": [],
                "meeting_dates": [],
                "ivod_committees": [],
                "gazette_queries": [],
            }

    
    # ================================================================
    # 3. IVOD 發言片段爬取 (優先使用 API，API 無資料時才爬蟲)
    # ================================================================

    def _date_to_roc(self, ad_date_str):
        """將 西元日期 (2024-12-13) 轉為 民國日期 (113/12/13)"""
        try:
            d = ad_date_str.replace("/", "-")
            dt = datetime.strptime(d, "%Y-%m-%d")
            roc_year = dt.year - 1911
            return f"{roc_year}/{dt.month:02d}/{dt.day:02d}"
        except:
            return ad_date_str

    def fetch_speeches_via_api(self, date_str, committee_name):
        """
        使用 ID 421 API 抓取指定日期與委員會的所有發言紀錄
        """
        roc_date = self._date_to_roc(date_str)
        api_url = "https://data.ly.gov.tw/odw/ID421Action.action"
        params = {
            "meetingDateS": roc_date,
            "meetingDateE": roc_date,
            "fileType": "csv"
        }
        
        try:
            # 使用 identity 避免 gzip 解碼問題
            headers = self.session.headers.copy()
            headers["Accept-Encoding"] = "identity"
            
            response = requests.get(api_url, params=params, headers=headers, timeout=30, verify=False)
            if response.status_code != 200 or len(response.content) < 500:
                return None
                
            # 解析 CSV (帶 BOM 的 UTF-8)
            content = response.content.decode('utf-8-sig')
            f = io.StringIO(content)
            reader = csv.DictReader(f)
            
            speeches = []
            session_tuple = None
            
            for row in reader:
                # 篩選委員會 (meetingTypeName)
                m_type = row.get('meetingTypeName', '')
                if committee_name and committee_name not in m_type and m_type not in committee_name:
                    continue
                
                # 萃取 session_tuple (從 meetingName)
                m_name = row.get('meetingName', '')
                if not session_tuple and m_name:
                    term_match = re.search(r'第(\d+)屆\s*第(\d+)會期\s*第(\d+)次', m_name)
                    if term_match:
                        session_tuple = (int(term_match.group(1)), int(term_match.group(2)), int(term_match.group(3)))
                
                # 格式轉換
                s_start = row.get('speechStartTime', '')
                s_end = row.get('speechEndTime', '')
                
                # 取得 speech_id
                speech_url = row.get('speechRecordUrl', '')
                speech_id = ""
                if speech_url:
                    id_match = re.search(r'/Speech/(\d+)', speech_url)
                    if id_match:
                        speech_id = id_match.group(1)
                
                speeches.append({
                    "legislator_name": row.get('legislatorName', ''),
                    "speech_time": f"{s_start} - {s_end}" if s_start and s_end else s_start,
                    "meeting_time": row.get('meetingDate', '') + " " + row.get('meetingTime', ''),
                    "meeting_name": m_name,
                    "speech_id": speech_id,
                })
            
            if not speeches:
                return None
                
            return {
                "session_tuple": session_tuple,
                "speeches": speeches
            }
        except Exception as e:
            print(f"API Fetch Error (ID 421): {e}")
            return None

    def _speech_from_id421_row(self, row):
        """Normalize one ID421 CSV row to the internal speech shape."""
        s_start = row.get('speechStartTime', '')
        s_end = row.get('speechEndTime', '')
        speech_url = row.get('speechRecordUrl', '')
        speech_id = ""
        if speech_url:
            id_match = re.search(r'/Speech/(\d+)', speech_url)
            if id_match:
                speech_id = id_match.group(1)

        return {
            "legislator_name": row.get('legislatorName', ''),
            "speech_time": f"{s_start} - {s_end}" if s_start and s_end else s_start,
            "meeting_time": (row.get('meetingDate', '') + " " + row.get('meetingTime', '')).strip(),
            "meeting_name": row.get('meetingName', ''),
            "meeting_type_name": row.get('meetingTypeName', ''),
            "speech_id": speech_id,
            "speech_record_url": speech_url,
            "video_url": row.get('videoUrl', ''),
        }

    @staticmethod
    def _name_matches(expected_name, actual_name):
        expected_name = (expected_name or "").strip()
        actual_name = (actual_name or "").strip()
        return bool(expected_name and actual_name and (expected_name in actual_name or actual_name in expected_name))

    @staticmethod
    def _session_matches(row, term, session_period, session_times):
        """Best-effort guard for API rows when the endpoint ignores session params."""
        meeting_name = row.get('meetingName', '') or ''
        row_term = row.get('term', '') or row.get('selectTerm', '')
        row_session = row.get('sessionPeriod', '')

        if row_term and str(row_term) != str(term):
            return False
        if row_session and str(row_session) != str(session_period):
            return False

        if not meeting_name:
            return True

        return str(term) in meeting_name and str(session_period) in meeting_name

    @staticmethod
    def _keyword_matches(row, bill_keywords):
        if not bill_keywords:
            return True
        haystack = " ".join([
            row.get('meetingContent', '') or '',
            row.get('meetingName', '') or '',
            row.get('meetingTypeName', '') or '',
        ])
        return any(keyword and keyword in haystack for keyword in bill_keywords)

    @staticmethod
    def _meeting_scope_matches(row, meeting_scopes):
        if not meeting_scopes:
            return True

        meeting_type = row.get('meetingTypeName', '') or ''
        meeting_name = row.get('meetingName', '') or ''
        scopes = set(meeting_scopes)

        if "院會" in scopes and meeting_type == "院會":
            return True
        if "委員會" in scopes:
            is_committee = "委員會" in meeting_type and meeting_type != "程序委員會"
            if is_committee:
                return True
            if "委員會" in meeting_name and "院會" not in meeting_type:
                return True

        return False

    @staticmethod
    def _committee_name_matches(row, committee_names):
        if not committee_names:
            return True

        meeting_type = row.get('meetingTypeName', '') or ''
        meeting_name = row.get('meetingName', '') or ''
        for committee_name in committee_names:
            if not committee_name:
                continue
            if committee_name in meeting_type or meeting_type in committee_name:
                return True
            if committee_name in meeting_name:
                return True
        return False

    @staticmethod
    def _session_date_range(term, session_period):
        """Estimate the regular session date range and return ROC date strings."""
        term = int(term)
        session_period = int(session_period)
        term_start_year = 2024 + (term - 11) * 4
        year = term_start_year + (session_period - 1) // 2

        if session_period % 2 == 1:
            start = datetime(year, 2, 1)
            end = datetime(year, 6, 30)
        else:
            start = datetime(year, 9, 1)
            end = datetime(year + 1, 1, 31)

        return (
            f"{start.year - 1911}/{start.month:02d}/{start.day:02d}",
            f"{end.year - 1911}/{end.month:02d}/{end.day:02d}",
        )

    def _fetch_id421_csv_rows(self, params):
        api_url = "https://data.ly.gov.tw/odw/ID421Action.action"
        headers = self.session.headers.copy()
        headers["Accept-Encoding"] = "identity"

        response = requests.get(api_url, params=params, headers=headers, timeout=60, verify=False)
        if response.status_code != 200 or len(response.content) < 20:
            return []

        content = response.content.decode('utf-8-sig')
        return list(csv.DictReader(io.StringIO(content)))

    def fetch_speeches_by_session_speakers(self, term, session_period, session_times, speakers, meeting_dates=None, bill_keywords=None, meeting_scopes=None):
        """
        Query ID421 by legislative session and speaker names.

        The gazette PDF gives the target speakers for a bill agenda. This method
        uses those speakers with the session tuple to get the IVOD speech URLs
        directly from the official API, avoiding IVOD list-page crawling.
        """
        if not speakers:
            return {
                "session_tuple": (term, session_period, session_times),
                "speeches": []
            }

        speeches = []
        seen = set()
        meeting_dates = meeting_dates or []

        query_windows = []
        for meeting_date in meeting_dates:
            roc_date = self._date_to_roc(meeting_date)
            query_windows.append((roc_date, roc_date))
        if not query_windows:
            query_windows.append(self._session_date_range(term, session_period))

        for speaker in sorted(set(speakers)):
            rows = []
            for start_date, end_date in query_windows:
                params = {
                    "meetingDateS": start_date,
                    "meetingDateE": end_date,
                    "legislatorName": speaker,
                    "fileType": "csv"
                }
                rows.extend(self._fetch_id421_csv_rows(params))
                time.sleep(0.1)

            candidate_rows = []
            for row in rows:
                actual_speaker = row.get('legislatorName', '')
                if not self._name_matches(speaker, actual_speaker):
                    continue
                if not self._session_matches(row, term, session_period, session_times):
                    continue
                if not self._meeting_scope_matches(row, meeting_scopes):
                    continue
                candidate_rows.append(row)

            keyword_rows = [
                row for row in candidate_rows
                if self._keyword_matches(row, bill_keywords)
            ]
            if bill_keywords and keyword_rows:
                candidate_rows = keyword_rows

            for row in candidate_rows:
                actual_speaker = row.get('legislatorName', '')

                speech_url = row.get('speechRecordUrl', '')
                dedupe_key = (
                    speech_url,
                    actual_speaker,
                    row.get('meetingDate', ''),
                    row.get('speechStartTime', ''),
                )
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                speeches.append(self._speech_from_id421_row(row))

            time.sleep(0.2)

        return {
            "session_tuple": (term, session_period, session_times),
            "speeches": speeches
        }

    def fetch_speeches_by_dates(self, term, session_period, meeting_dates, bill_keywords=None, meeting_scopes=None, committee_names=None):
        """
        Query ID421 by meeting dates parsed from a PPG bill page.

        This catches committee discussion records when the PPG page has IVOD
        dates but the gazette speaker index is unavailable or too broad.
        """
        if not meeting_dates:
            return {
                "session_tuple": (term, session_period, None),
                "speeches": []
            }

        speeches = []
        seen = set()

        for meeting_date in meeting_dates:
            roc_date = self._date_to_roc(meeting_date)
            rows = self._fetch_id421_csv_rows({
                "meetingDateS": roc_date,
                "meetingDateE": roc_date,
                "fileType": "csv"
            })

            keyword_rows = [
                row for row in rows
                if self._session_matches(row, term, session_period, None)
                and self._meeting_scope_matches(row, meeting_scopes)
                and self._committee_name_matches(row, committee_names)
                and self._keyword_matches(row, bill_keywords)
            ]
            candidate_rows = keyword_rows

            if not candidate_rows:
                candidate_rows = [
                    row for row in rows
                    if self._session_matches(row, term, session_period, None)
                    and self._meeting_scope_matches(row, meeting_scopes)
                    and self._committee_name_matches(row, committee_names)
                ]

            for row in candidate_rows:
                actual_speaker = row.get('legislatorName', '')
                speech_url = row.get('speechRecordUrl', '')
                dedupe_key = (
                    speech_url,
                    actual_speaker,
                    row.get('meetingDate', ''),
                    row.get('speechStartTime', ''),
                )
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                speeches.append(self._speech_from_id421_row(row))

            time.sleep(0.2)

        return {
            "session_tuple": (term, session_period, None),
            "speeches": speeches
        }

    def fetch_ivod_speech_list(self, ivod_newsclip_url):
        """
        從 IVOD 取得發言名單。
        策略：優先嘗試 Open Data API (ID 421)，失敗或無資料時才使用爬蟲。
        """
        # 1. 嘗試從 URL 萃取日期與委員會
        date_match = re.search(r'Querydate=([\d-]+)', ivod_newsclip_url)
        comm_match = re.search(r'Committeename=([^&]+)', ivod_newsclip_url)
        
        if date_match:
            date_str = date_match.group(1)
            import urllib.parse
            comm_name = urllib.parse.unquote(comm_match.group(1)) if comm_match else ""
            
            # 嘗試 API
            api_result = self.fetch_speeches_via_api(date_str, comm_name)
            if api_result and api_result['speeches']:
                return api_result

        # 2. API 失敗或無資料，進入爬蟲模式
        speeches = []
        session_tuple = None

        def parse_page(soup):
            nonlocal session_tuple
            clip_divs = soup.find_all('div', class_='clip-list-text')
            page_speeches = []
            for div in clip_divs:
                text = div.get_text(separator='\n', strip=True)
                if '委員：' not in text:
                    continue
                name_match = re.search(r'委員[：:]\s*(.+?)(?:\n|委員發言)', text)
                legislator_name = name_match.group(1).strip() if name_match else ""
                time_match = re.search(r'委員發言時間[：:]\s*(.+?)(?:\n|影片)', text)
                speech_time = time_match.group(1).strip() if time_match else ""
                meeting_time_match = re.search(r'會議時間[：:]\s*(.+?)(?:\n|會議名稱)', text)
                meeting_time = meeting_time_match.group(1).strip() if meeting_time_match else ""
                meeting_name_match = re.search(r'會議名稱[：:]\s*(.+?)(?:公報連結|$)', text, re.DOTALL)
                meeting_name = meeting_name_match.group(1).strip() if meeting_name_match else ""
                # 從 meeting_name 萃取屆期會次
                if not session_tuple and meeting_name:
                    term_match = re.search(r'第(\d+)屆\s*第(\d+)會期\s*第(\d+)次', meeting_name)
                    if term_match:
                        session_tuple = (int(term_match.group(1)), int(term_match.group(2)), int(term_match.group(3)))
                if len(meeting_name) > 80:
                    meeting_name = meeting_name[:80] + "..."
                speech_id = ""
                for a in div.find_all('a'):
                    href = a.get('href', '')
                    if 'Demand/Speech' in href:
                        id_match = re.search(r'/Speech/(\d+)', href)
                        if id_match:
                            speech_id = id_match.group(1)
                            break
                if legislator_name:
                    page_speeches.append({
                        "legislator_name": legislator_name,
                        "speech_time": speech_time,
                        "meeting_time": meeting_time,
                        "meeting_name": meeting_name,
                        "speech_id": speech_id,
                    })
            return page_speeches

        try:
            # 先抓第一頁，同時取得總筆數
            response = self._get(ivod_newsclip_url, timeout=(10, 30))
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')

            # 抓取總筆數 (「搜尋結果：...之搜尋結果為 30 筆」)
            total_count = 0
            data_list_div = soup.find('div', class_='data-list')
            if data_list_div:
                total_text = data_list_div.get_text()
                total_match = re.search(r'搜尋結果為\s*(\d+)\s*筆', total_text)
                if total_match:
                    total_count = int(total_match.group(1))

            # 解析第一頁
            page1_speeches = parse_page(soup)
            speeches.extend(page1_speeches)
            per_page = len(page1_speeches) if page1_speeches else 6

            # 計算需要爬取的總頁數
            if total_count > per_page and per_page > 0:
                import math
                total_pages = math.ceil(total_count / per_page)
                for pg in range(2, total_pages + 1):
                    try:
                        paged_url = f"{ivod_newsclip_url}&page={pg}"
                        r = self._get(paged_url, timeout=(10, 30))
                        r.raise_for_status()
                        pg_soup = BeautifulSoup(r.text, 'html.parser')
                        pg_speeches = parse_page(pg_soup)
                        if not pg_speeches:
                            break  # 已到最後一頁
                        speeches.extend(pg_speeches)
                        time.sleep(0.2)
                    except Exception:
                        break

        except Exception as e:
            print(f"Error fetching IVOD speech list: {e}")

        return {
            "session_tuple": session_tuple,
            "speeches": speeches
        }

    # ================================================================
    # 4. 發言純文字擷取 (IVOD Demand/Speech)
    # ================================================================
    
    def fetch_transcript(self, speech_id_or_url):
        """
        根據 Speech ID 或 speechRecordUrl 從 IVOD 擷取發言純文字
        """
        if not speech_id_or_url:
            return ""
        
        # 處理完整 URL 或 ID
        if speech_id_or_url.startswith("http"):
            url = speech_id_or_url
        else:
            url = f"https://ivod.ly.gov.tw/Demand/Speech/{speech_id_or_url}"
        
        try:
            response = self._get(url, timeout=(10, 30))
            response.raise_for_status()
            response.encoding = 'utf-8'
            
            soup = BeautifulSoup(response.text, 'html.parser')
            text = soup.get_text(separator='\n').strip()
            return text
        except Exception as e:
            return f"[無法取得發言內容: {e}]"

    # ================================================================
    # 5. 從法案名稱中擷取關鍵字 (用於 meetingContent 比對)
    # ================================================================
    
    @staticmethod
    def extract_bill_keywords(bill_name):
        """
        從法案名稱中擷取用來比對議程內容的關鍵字
        例如 '《立法院職權行使法》部分條文' → ['職權行使法']
        例如 '中華民國114年度中央政府總預算案' → ['總預算']
        """
        keywords = []
        
        # 優先尋找書名號內的文字 (通常是最準確的法案名稱)
        bracket_matches = re.findall(r'[《「](.*?)[》」]', bill_name)
        if bracket_matches:
            for m in bracket_matches:
                # 移除"草案", "部分條文"等贅字
                clean_m = re.sub(r'部分條文.*$|草案.*$|修正.*$', '', m)
                if clean_m and clean_m not in keywords:
                    keywords.append(clean_m)
                    # 同時嘗試萃取最根本的法律名稱 (例如「立法院組織法」)
                    base_law = re.search(r'([\u4e00-\u9fff]{2,10}法|[\u4e00-\u9fff]{2,10}條例)', clean_m)
                    if base_law and base_law.group(1) not in keywords:
                        keywords.append(base_law.group(1))
        
        if not keywords:
            # 移除常見贅字 (不要用 .*? 因為會把中間的字全吃掉)
            clean = re.sub(r'審查|擬具|報告|討論|事項|黨團|委員|院會|併案|報告彙總完成|總報告', '', bill_name)
            # 移除書名號
            clean = re.sub(r'[《》「」\(\)（）]', '', clean)
            
            # 使用較嚴謹的模式，避免抓到整句話
            # 尋找 2-10 個中文字加上法或條例
            patterns = [
                r'([\u4e00-\u9fff]{2,10}法)',
                r'([\u4e00-\u9fff]{2,10}條例)',
                r'(總預算)',
            ]
            
            for p in patterns:
                matches = re.findall(p, clean)
                for m in matches:
                    if m not in keywords:
                        keywords.append(m)
        
        # 針對一些簡稱做處理
        final_keywords = []
        for kw in keywords:
            final_keywords.append(kw)
            if "財政收支劃分法" in kw:
                final_keywords.append("財劃法")
            if "立法院職權行使法" in kw:
                final_keywords.append("國會改革")
            if "公職人員選舉罷免法" in kw:
                final_keywords.append("選罷法")
                
        # 如果都沒找到，取前5個中文字
        if not final_keywords:
            clean = re.sub(r'審查|擬具|報告|討論|事項|黨團|委員|院會|併案|草案|修正|部分條文', '', bill_name)
            chinese_only = re.sub(r'[^\u4e00-\u9fff]', '', clean)
            if chinese_only:
                final_keywords = [chinese_only[:5]]
            else:
                final_keywords = [bill_name[:5]]
        
        return final_keywords

    # ================================================================
    # 6. 公報發言索引 PDF 解析 (分離議程)
    # ================================================================
    
    def fetch_gazette_index_pdfs(self, term, session_period, session_times):
        """
        透過立法院 API 獲取特定會次的公報索引 PDF 連結
        """
        url = f"https://ppg.ly.gov.tw/ppg/api/v1/publication?size=10&page=1&sortCode=01&publicationType=7&term={term}&sessionPeriod={session_period}&sessionTimes={session_times}&queryType=0"
        pdf_urls = []
        try:
            response = self._get(url, timeout=(10, 30))
            response.raise_for_status()
            data = response.json()
            for item in data.get("items", []):
                for attachment in item.get("attachments", []):
                    if attachment.get("attachmentType") == "PDF":
                        pdf_urls.append(attachment.get("link"))
        except Exception as e:
            print(f"Error fetching gazette API: {e}")
            
        return list(set(pdf_urls))

    def parse_gazette_pdf_for_speakers(self, pdf_url, bill_keywords):
        """
        下載並解析公報發言索引 PDF，比對法案關鍵字，回傳符合議程的發言委員名單
        """
        speakers = set()
        try:
            response = self._get(pdf_url, timeout=(10, 60))
            response.raise_for_status()
            
            with pdfplumber.open(io.BytesIO(response.content)) as pdf:
                for page in pdf.pages:
                    text = page.extract_text()
                    if not text:
                        continue
                    
                    # 尋找有符合關鍵字的行數
                    lines = text.split('\n')
                    for i, line in enumerate(lines):
                        keyword_match = False
                        for kw in bill_keywords:
                            if kw in line:
                                keyword_match = True
                                break
                                
                        if keyword_match:
                            # 找到議程後，往下找最近的一個 "發 言 者" 或 "發言者"
                            j = i + 1
                            found_speakers = False
                            while j < len(lines):
                                current_line = lines[j]
                                # 如果在找到發言者前就遇到下一個議程的結尾(頁碼)等，可能這議程沒發言者
                                if re.match(r'^\d+$', current_line.strip()):
                                    break
                                    
                                if '發 言 者' in current_line or '發言者' in current_line:
                                    names_text = current_line.replace('發 言 者', '').replace('發言者', '').strip()
                                    
                                    # 繼續往下讀取可能換行的名單
                                    k = j + 1
                                    while k < len(lines):
                                        if re.match(r'^\d+$', lines[k].strip()) or '發 言 者' in lines[k] or '發言者' in lines[k]:
                                            break
                                        # 如果下一行似乎是新議程標題(很長且不含名字常見字元)，也要考慮中斷
                                        names_text += " " + lines[k].strip()
                                        k += 1
                                        
                                    names_text = re.sub(r'（.*?）|\(.*?\)', '', names_text)
                                    names_text = names_text.replace('、', ' ')
                                    
                                    for name_part in names_text.split():
                                        if name_part.strip():
                                            clean_name = re.sub(r'[a-zA-Z\s]+', '', name_part)
                                            # 過濾掉長度小於2或大於4的不合理名字，或是"完成三讀"等非名字
                                            if clean_name and len(clean_name) >= 2 and len(clean_name) <= 4 and "三讀" not in clean_name and "修正" not in clean_name:
                                                speakers.add(clean_name)
                                    found_speakers = True
                                    break
                                j += 1
                                
                            if found_speakers:
                                # 繼續找下一個可能出現的同法案關鍵字段落
                                pass
                                
        except Exception as e:
            print(f"Error parsing PDF {pdf_url}: {e}")
            
        return list(speakers)
