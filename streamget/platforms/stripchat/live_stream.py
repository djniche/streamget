import json
import re

from ...data import StreamData, wrap_stream
from ...requests.async_http import async_req
from ..base import BaseLiveStream

# Stripchat 的分片地址被混淆在 #EXT-X-MOUFLON 标签里，ffmpeg 直接读会拿到广告占位流。
# 这里只负责解析出「带 psch/pkey 的变体地址」，解码由调用方（StreamCap）完成。
EDGE_HOST = 'edge-hls.doppiocdn.com'

# viewCam.model.status 的取值
_LIVE_STATUS = 'public'
_PRIVATE_STATUS = {
    'private': '私密秀中，无法录制',
    'groupShow': '群秀中，无法录制',
    'p2p': 'P2P 私密秀中，无法录制',
    'virtualPrivate': '虚拟私密秀中，无法录制',
    'p2pVoice': '语音私密中，无法录制',
    'off': '主播离线',
    'idle': '主播暂离',
}


class StripchatLiveStream(BaseLiveStream):
    """
    A class for fetching and processing Stripchat live stream information.
    """

    def __init__(self, proxy_addr: str | None = None, cookies: str | None = None):
        super().__init__(proxy_addr, cookies)
        self.pc_headers = self._get_pc_headers()

    def _get_pc_headers(self) -> dict:
        return {
            'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'sec-fetch-dest': 'document',
            'sec-fetch-mode': 'navigate',
            'sec-fetch-site': 'none',
            'upgrade-insecure-requests': '1',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            'cookie': self.cookies or '',
        }

    @staticmethod
    def _parse_room(url: str) -> str:
        """从 https://zh.stripchat.com/<user> 取出用户名"""
        path = url.split('?')[0].split('#')[0].rstrip('/')
        name = path.rsplit('/', 1)[-1]
        return name or ''

    @staticmethod
    def _parse_state(html: str) -> dict:
        m = re.search(r'window\.__PRELOADED_STATE__\s*=\s*(\{.*?\});?\s*</script>', html, re.S)
        if not m:
            return {}
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _pick_variant(master: str) -> tuple:
        """返回 (最高码率变体 URL, BANDWIDTH)"""
        best, variants = None, []
        bandwidth = 0
        for line in master.splitlines():
            if line.startswith('#EXT-X-STREAM-INF:'):
                m = re.search(r'BANDWIDTH=(\d+)', line)
                bandwidth = int(m.group(1)) if m else 0
            elif line.startswith('http'):
                variants.append((bandwidth, line.strip()))
        if not variants:
            return None, 0
        variants.sort(key=lambda x: x[0], reverse=True)
        best = variants[0]
        return best[1], best[0]

    async def fetch_web_stream_data(self, url: str, process_data: bool = True) -> dict:
        username = self._parse_room(url)
        if not username:
            raise RuntimeError(f'Stripchat 地址解析失败: {url}')

        html = await async_req(
            f'https://stripchat.com/{username}/',
            proxy_addr=self.proxy_addr,
            headers=self.pc_headers,
        )
        if not html:
            raise RuntimeError(f'Stripchat 页面获取失败: {username}')

        state = self._parse_state(html)
        if not process_data:
            return state

        view = state.get('viewCam') or {}
        model = view.get('model') or {}
        status = model.get('status')
        model_id = model.get('id')
        is_live = bool(model.get('isLive')) and status == _LIVE_STATUS and bool(model_id)

        result = {
            'anchor_name': username,
            'is_live': is_live,
            'live_url': url,
            'title': model.get('username') or username,
            'model_id': model_id,
            'status': status,
        }
        if not is_live:
            return result

        master_url = f'https://{EDGE_HOST}/hls/{model_id}/master/{model_id}_auto.m3u8'
        master = await async_req(master_url, proxy_addr=self.proxy_addr, headers={
            'user-agent': self.pc_headers['user-agent'],
            'referer': f'https://stripchat.com/{username}/',
            'accept': '*/*',
        })
        if not master or '#EXT-X-STREAM-INF' not in master:
            result['is_live'] = False
            return result

        variant, bandwidth = self._pick_variant(master)
        pkeys = re.findall(r'#EXT-X-MOUFLON:PSCH:v2:([A-Za-z0-9]+)', master)
        if not variant or not pkeys:
            result['is_live'] = False
            return result

        sep = '&' if '?' in variant else '?'
        play_url = f'{variant}{sep}psch=v2&pkey={pkeys[0]}'
        result['m3u8_url'] = play_url
        result['bandwidth'] = bandwidth
        result['pkey'] = pkeys[0]
        result['record_url'] = play_url
        return result

    async def fetch_stream_url(self, json_data: dict, video_quality: str | int | None = None) -> StreamData:
        if not json_data.get('is_live'):
            json_data |= {'platform': 'Stripchat'}
            return wrap_stream(json_data)

        quality, _ = self.get_quality_index(video_quality)
        return wrap_stream({
            'platform': 'Stripchat',
            'anchor_name': json_data.get('anchor_name'),
            'is_live': True,
            'live_url': json_data.get('live_url'),
            'title': json_data.get('title'),
            'quality': quality,
            'm3u8_url': json_data.get('m3u8_url'),
            'record_url': json_data.get('record_url'),
            'extra': {'pkey': json_data.get('pkey')},
        })
