import json
import random
import re
from urllib.parse import urljoin

from ...data import StreamData, wrap_stream
from ...requests.async_http import async_req
from ..base import BaseLiveStream

MFC_API = 'https://api-edge.myfreecams.com/usernameLookup/{model}'
MFC_EDGE = 'https://edgevideo.myfreecams.com'

# vstate: 0/90 直播中；127 离线；12 私密；13 群秀；2 离开
_LIVE_STATES = (0, 90)
_STATE_MSG = {
    127: '主播离线',
    12: '私密秀中，无法录制',
    13: '群秀中，无法录制',
    2: '主播暂离',
}


class MyFreeCamsLiveStream(BaseLiveStream):
    """
    A class for fetching and processing MyFreeCams live stream information.
    """

    def __init__(self, proxy_addr: str | None = None, cookies: str | None = None):
        super().__init__(proxy_addr, cookies)
        self.pc_headers = self._get_pc_headers()

    def _get_pc_headers(self) -> dict:
        return {
            'accept': 'application/json, text/plain, */*',
            'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'referer': 'https://www.myfreecams.com/',
            'origin': 'https://www.myfreecams.com',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            'cookie': self.cookies or '',
        }

    @staticmethod
    def _parse_model(url: str) -> str:
        """兼容 www/share/app/m 四种域名和 #name 写法"""
        u = url.split('?')[0].rstrip('/')
        if '#' in u:
            return u.rsplit('#', 1)[-1]
        return u.rsplit('/', 1)[-1]

    async def _get_play_url_list(self, m3u8_url: str) -> list:
        resp = await async_req(m3u8_url, proxy_addr=self.proxy_addr, headers=self.pc_headers)
        if not resp or '#EXT-X-STREAM-INF' not in resp:
            return [m3u8_url]

        lines = resp.splitlines()
        pairs = []
        for i, line in enumerate(lines):
            if not line.startswith('#EXT-X-STREAM-INF'):
                continue
            m = re.search(r'BANDWIDTH=(\d+)', line)
            bandwidth = int(m.group(1)) if m else 0
            for nxt in lines[i + 1:]:
                if nxt.strip() and not nxt.startswith('#'):
                    pairs.append((bandwidth, urljoin(m3u8_url, nxt.strip())))
                    break
        if not pairs:
            return [m3u8_url]
        pairs.sort(key=lambda x: x[0], reverse=True)
        return [u for _, u in pairs]

    async def fetch_web_stream_data(self, url: str, process_data: bool = True) -> dict:
        """
        Fetches web stream data for a live room.
        """
        model = self._parse_model(url)
        text = await async_req(url=MFC_API.format(model=model), proxy_addr=self.proxy_addr,
                               headers=self.pc_headers)
        if not text:
            raise RuntimeError(f'MyFreeCams 无响应: {url}')

        json_data = json.loads(text)
        if not process_data:
            return json_data

        user = (json_data.get('result') or {}).get('user') or {}
        if not user:
            raise RuntimeError(f'MyFreeCams 找不到主播: {model}')

        anchor_name = user.get('username') or model
        sessions = [s for s in (user.get('sessions') or []) if s.get('server_name')]
        state = sessions[0].get('vstate') if sessions else None
        title = (sessions[0].get('room_topic') or '').strip() if sessions else ''

        result = {
            'anchor_name': anchor_name,
            'is_live': False,
            'live_url': url,
            'title': title or anchor_name,
        }
        if not sessions or state not in _LIVE_STATES:
            return result

        server_id = sessions[0]['server_name'][5:]          # video878 -> 878
        phase = sessions[0].get('phase') or ''
        mid = int(user['id']) + 100_000_000
        rnd = random.random()
        m3u8_url = (f'{MFC_EDGE}/hls/NxServer/{server_id}/ngrp:mfc_{phase}{mid}'
                    f'.f4v_mobile/playlist.m3u8?nc={rnd}&v=1.97.23')

        result |= {
            'is_live': True,
            'm3u8_url': m3u8_url,
            'play_url_list': await self._get_play_url_list(m3u8_url),
        }
        return result

    async def fetch_stream_url(self, json_data: dict, video_quality: str | int | None = None) -> StreamData:
        """
        Fetches the stream URL for a live room and wraps it into a StreamData object.
        """
        if not json_data.get('is_live'):
            json_data |= {'platform': 'MyFreeCams'}
            return wrap_stream(json_data)

        data = await self.get_stream_url(json_data, video_quality, platform='MyFreeCams')
        return wrap_stream(data)
