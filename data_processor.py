import pandas as pd
import zipfile
import io
import re
import time

class DataProcessor:
    """資料處理與匯出工具"""
    
    def __init__(self, api_client):
        self.api_client = api_client

    def build_dataframe_from_api(self, api_items, bill_short_name=""):
        """
        將 API 148 回傳的 items 轉為 DataFrame
        """
        if not api_items:
            return pd.DataFrame()
        
        rows = []
        for item in api_items:
            speech_record_url = item.get('speechRecordUrl', '')
            # 從 speechRecordUrl 擷取 speech_id
            speech_id = ""
            if speech_record_url:
                match = re.search(r'/Speech/(\d+)', speech_record_url)
                if match:
                    speech_id = match.group(1)
            
            rows.append({
                "法案名稱": bill_short_name,
                "會議類型": item.get("meetingTypeName", ""),
                "委員姓名": item.get("legislatorName", ""),
                "選區": item.get("areaName", ""),
                "會議日期": item.get("meetingDate", ""),
                "會議名稱": item.get("meetingName", ""),
                "議程內容": (item.get("meetingContent", "") or "")[:100],
                "發言起始": item.get("speechStartTime", ""),
                "發言結束": item.get("speechEndTime", ""),
                "影片長度": item.get("videoLength", ""),
                "speech_id": speech_id,
                "speechRecordUrl": speech_record_url,
                "videoUrl": item.get("videoUrl", ""),
            })
        
        df = pd.DataFrame(rows)
        # 去重
        if not df.empty:
            df = df.drop_duplicates(subset=['委員姓名', '發言起始', '會議日期'])
        return df

    def build_dataframe_from_ivod(self, ivod_speeches, bill_short_name="", bill_url=""):
        """
        將 IVOD 爬蟲的 speeches 轉為 DataFrame
        """
        if not ivod_speeches:
            return pd.DataFrame()
        
        rows = []
        for speech in ivod_speeches:
            rows.append({
                "法案名稱": bill_short_name,
                "會議類型": speech.get("meeting_type_name", ""),
                "委員姓名": speech.get("legislator_name", ""),
                "選區": "",
                "會議日期": speech.get("meeting_time", "").split(" ")[0] if speech.get("meeting_time") else "",
                "會議名稱": speech.get("meeting_name", ""),
                "議程內容": "",
                "發言起始": speech.get("speech_time", "").split(" - ")[0] if speech.get("speech_time") else "",
                "發言結束": speech.get("speech_time", "").split(" - ")[-1] if speech.get("speech_time") else "",
                "影片長度": "",
                "speech_id": speech.get("speech_id", ""),
                "speechRecordUrl": speech.get("speech_record_url") or (f"https://ivod.ly.gov.tw/Demand/Speech/{speech.get('speech_id', '')}" if speech.get("speech_id") else ""),
                "videoUrl": speech.get("video_url", ""),
            })
        
        df = pd.DataFrame(rows)
        if not df.empty:
            df = df.drop_duplicates(subset=['委員姓名', '發言起始', '會議日期'])
        return df

    def filter_by_legislator(self, df, legislator_name):
        """根據委員姓名過濾"""
        if df.empty or not legislator_name:
            return df
        return df[df['委員姓名'].str.contains(legislator_name, case=False, na=False)]

    def sanitize_filename(self, text, max_len=30):
        """清理檔名中不允許的字元"""
        cleaned = re.sub(r'[\\/:*?"<>|\n\r]', '', text)
        cleaned = cleaned.strip()
        if len(cleaned) > max_len:
            cleaned = cleaned[:max_len]
        return cleaned

    def generate_filename(self, row):
        """生成檔名：法案名稱_會議日期_委員姓名"""
        bill = self.sanitize_filename(row.get('法案名稱', ''), 20)
        meeting = self.sanitize_filename(row.get('會議日期', ''), 15)
        legislator = self.sanitize_filename(row.get('委員姓名', ''), 10)
        filename = f"{bill}_{meeting}_{legislator}"
        return filename

    def export_to_csv(self, df):
        """將 DataFrame 匯出為 CSV"""
        csv_buffer = io.BytesIO()
        export_df = df.drop(columns=['speech_id', 'speechRecordUrl', 'videoUrl'], errors='ignore')
        export_df.to_csv(csv_buffer, index=False, encoding='utf-8-sig')
        return csv_buffer.getvalue()

    def export_single_txt(self, row, transcript):
        """產生單筆 TXT 檔案內容"""
        header = f"法案名稱：{row.get('法案名稱', '')}\n"
        header += f"委員姓名：{row.get('委員姓名', '')}\n"
        header += f"選區：{row.get('選區', '')}\n"
        header += f"會議日期：{row.get('會議日期', '')}\n"
        header += f"會議名稱：{row.get('會議名稱', '')}\n"
        header += f"議程內容：{row.get('議程內容', '')}\n"
        header += f"發言時間：{row.get('發言起始', '')} - {row.get('發言結束', '')}\n"
        header += "=" * 60 + "\n\n"
        return header + transcript

    def export_to_zip(self, df, progress_callback=None):
        """將多筆發言的純文字檔打包成 ZIP"""
        zip_buffer = io.BytesIO()
        total = len(df)
        
        with zipfile.ZipFile(zip_buffer, "a", zipfile.ZIP_DEFLATED, False) as zip_file:
            for i, (idx, row) in enumerate(df.iterrows()):
                speech_url = row.get('speechRecordUrl', '')
                speech_id = row.get('speech_id', '')
                
                transcript = self.api_client.fetch_transcript(speech_url or speech_id)
                
                if not transcript:
                    transcript = "無法取得發言內容或影片網址不存在。"
                
                content = self.export_single_txt(row, transcript)
                filename = self.generate_filename(row) + ".txt"
                zip_file.writestr(filename, content.encode('utf-8'))
                
                if progress_callback:
                    progress_callback((i + 1) / total)
                
                time.sleep(0.2)
                
        return zip_buffer.getvalue()
