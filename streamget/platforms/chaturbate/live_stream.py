import json
import re
from urllib.parse import urljoin

from ...data import StreamData, wrap_stream
from ...requests.async_http import async_req
from ..base import BaseLiveStream


class ChaturbateLiveStream(BaseLiveStream):
    """
    A class for fetching and processing Chaturbate live stream information.

    CB 的 LL-HLS master 里音频是独立的 EXT-X-MEDIA 轨道（fMP4/CMAF）。
    ffmpeg 5.1 的 HLS 解复用器同时选视频和音频时**不会拉音频分片**
    （实测音频包 0），所以这里拆成两个 URL，由 StreamCap 用两个 -i 输入录制：
    视频取最高码率变体，音频取它 AUDIO= 对应的那条轨道。
    """

    def __init__(self, proxy_addr: str | None = None, cookies: str | None = None):
        super().__init__(proxy_addr, cookies)
        self.pc_headers = self._get_pc_headers()

    def _get_pc_headers(self) -> dict:
        return {
            'accept': 'application/json, text/javascript, */*; q=0.01',
            'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'origin': 'https://chaturbate.com',
            'referer': 'https://chaturbate.com/',
            'x-requested-with': 'XMLHttpRequest',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            'cookie': self.cookies or '',
        }

    @staticmethod
    def _parse_room(url: str) -> str:
        """从 https://chaturbate.com/<user>/ 取出用户名"""
        path = url.split('?')[0].rstrip('/')
        room = path.rsplit('/', 1)[-1]
        if room == 'in':
            m = re.search(r'[?&]room=([^&]+)', url)
            if m:
                return m.group(1)
        return room

    @staticmethod
    def _attrs(line: str) -> dict:
        body = line.split(':', 1)[1] if ':' in line else ''
        return {m.group(1): m.group(2).strip('"')
                for m in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', body)}

    def _split_master(self, master_url: str, text: str) -> tuple:
        """返回 (最高码率视频变体 URL, 它对应的音频轨道 URL)"""
        audios, variants = {}, []
        lines = text.splitlines()

        for i, line in enumerate(lines):
            if line.startswith('#EXT-X-MEDIA:') and 'TYPE=AUDIO' in line:
                a = self._attrs(line)
                if a.get('GROUP-ID') and a.get('URI'):
                    audios.setdefault(a['GROUP-ID'], urljoin(master_url, a['URI']))
            elif line.startswith('#EXT-X-STREAM-INF:'):
                a = self._attrs(line)
                bandwidth = int(a.get('BANDWIDTH') or 0)
                for nxt in lines[i + 1:]:
                    if nxt.strip() and not nxt.startswith('#'):
                        variants.append((bandwidth, a.get('AUDIO'), urljoin(master_url, nxt.strip())))
                        break

        if not variants:
            return None, None

        variants.sort(key=lambda x: x[0], reverse=True)
        _, group, video_url = variants[0]
        return video_url, audios.get(group)

    async def fetch_web_stream_data(self, url: str, process_data: bool = True) -> dict:
        """
        Fetches web stream data for a live room.
        """
        room_slug = self._parse_room(url)
        api = 'https://chaturbate.com/get_edge_hls_url_ajax/'
        data = {'room_slug': room_slug, 'bandwidth': 'high'}

        text = await async_req(url=api, proxy_addr=self.proxy_addr, headers=self.pc_headers, data=data)
        if not text:
            raise RuntimeError(f'Chaturbate 无响应: {url}')
        json_data = json.loads(text)
        if not process_data:
            return json_data

        room_status = json_data.get('room_status')
        m3u8_url = json_data.get('url')
        is_live = room_status == 'public' and bool(m3u8_url)

        result = {
            'anchor_name': room_slug,
            'is_live': is_live,
            'live_url': url,
            'title': json_data.get('room_title') or room_slug,
        }
        if not is_live:
            return result

        if json_data.get('cmaf_edge'):
            m3u8_url = m3u8_url.replace('playlist.m3u8', 'playlist_sfm4s.m3u8')
            m3u8_url = re.sub(r'live-.+amlst', 'live-c-fhls/amlst', m3u8_url)

        result['m3u8_url'] = m3u8_url

        # 拆成「视频变体 + 音频轨道」两个输入
        master = await async_req(m3u8_url, proxy_addr=self.proxy_addr, headers=self.pc_headers)
        if master and '#EXT-X-STREAM-INF' in master:
            video_url, audio_url = self._split_master(m3u8_url, master)
            if video_url:
                result['m3u8_url'] = video_url
                result['record_url'] = video_url
                if audio_url:
                    result['extra'] = {'audio_url': audio_url}
                return result

        result['record_url'] = m3u8_url
        return result

    async def fetch_stream_url(self, json_data: dict, video_quality: str | int | None = None) -> StreamData:
        """
        Fetches the stream URL for a live room and wraps it into a StreamData object.
        """
        if not json_data.get('is_live'):
            json_data |= {'platform': 'Chaturbate'}
            return wrap_stream(json_data)

        quality, _ = self.get_quality_index(video_quality)
        return wrap_stream({
            'platform': 'Chaturbate',
            'anchor_name': json_data.get('anchor_name'),
            'is_live': True,
            'live_url': json_data.get('live_url'),
            'title': json_data.get('title'),
            'quality': quality,
            'm3u8_url': json_data.get('m3u8_url'),
            'record_url': json_data.get('record_url'),
            'extra': json_data.get('extra'),
        })
