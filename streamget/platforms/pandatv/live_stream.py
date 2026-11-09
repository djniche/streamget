import json
import re

from ...data import StreamData, wrap_stream
from ...requests.async_http import async_req
from ..base import BaseLiveStream


class PandaLiveStream(BaseLiveStream):
    """
    A class for fetching and processing PandaLive live stream information.
    """
    def __init__(self, proxy_addr: str | None = None, cookies: str | None = None):
        super().__init__(proxy_addr, cookies)
        self.pc_headers = self._get_pc_headers()
        self.m3u_list_url = 'https://5721004.xyz/player/list.m3u'

    def _get_pc_headers(self) -> dict:
        return {
            'origin': 'https://www.pandalive.co.kr',
            'referer': 'https://www.pandalive.co.kr/',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0',
            'cookie': self.cookies or '',
        }

    async def _fetch_stream_from_remote_m3u(self, user_id: str) -> tuple[str | None, bool]:
        """
        返回: (m3u_url, is_recorded)
        """
        try:
            m3u_content = await async_req(
                self.m3u_list_url,
                proxy_addr=self.proxy_addr,
                headers={'user-agent': self.pc_headers['user-agent']}
            )
            if not m3u_content:
                return None, False

            lines = [line.strip() for line in m3u_content.splitlines() if line.strip()]
            for i in range(len(lines)):
                line = lines[i]
                if line.startswith('#EXTINF:'):
                    inf_parts = line.split(',', 2)
                    if len(inf_parts) >= 2:
                        matched_id = inf_parts[1].strip()
                        if matched_id.lower() == user_id.lower():
                            is_recorded = '[录]' in line or '[녹]' in line
                            if i + 1 < len(lines) and lines[i + 1].startswith('http'):
                                return lines[i + 1], is_recorded
        except Exception:
            pass
        return None, False

    async def fetch_web_stream_data(self, url: str, process_data: bool = True) -> dict:
        user_id = url.split('?')[0].rsplit('/', maxsplit=1)[1]
        url2 = 'https://api.pandalive.co.kr/v1/live/play'
        data = {
            'userId': user_id,
            'info': 'media fanGrade',
        }
        room_password = self.get_params(url, "pwd") or ''
        data2 = {
            'action': 'watch',
            'userId': user_id,
            'password': room_password,
            'shareLinkType': '',
        }

        result = {"anchor_name": "", "is_live": False, "live_url": url}
        json_str = await async_req(
            'https://api.pandalive.co.kr/v1/member/bj',
            proxy_addr=self.proxy_addr,
            headers=self.pc_headers,
            data=data
        )
        json_data = json.loads(json_str)
        if not process_data:
            return json_data

        # 容错防崩：部分特殊状态下 bjInfo 缺失的处理
        if 'bjInfo' not in json_data:
            return result

        anchor_id = json_data['bjInfo']['id']
        anchor_name = f"{json_data['bjInfo']['nick']}-{anchor_id}"
        result['anchor_name'] = anchor_name
        live_status = 'media' in json_data

        if live_status:
            media_info = json_data.get('media', {})

            # 1. 官方字段与房间标题判定录播
            title = media_info.get('title', '')
            is_rec_official = (
                media_info.get('liveType') == 'rec'
                or media_info.get('onAirType') == 'rec'
                or '[녹]' in title
                or '[录]' in title
            )

            if is_rec_official:
                # 命中录播，直接返回 is_live=False（StreamCap 会判定为未开播，跳过不录）
                return result

            play_url = None
            is_m3u_stream = False

            # 2. 优先尝试从远程 M3U 拿流
            m3u_stream_url, is_m3u_rec = await self._fetch_stream_from_remote_m3u(user_id)
            
            # 如果远程 M3U 里将该主播标注为 [录]，同样判定为录播并跳过
            if is_m3u_rec:
                return result

            if m3u_stream_url:
                play_url = m3u_stream_url
                is_m3u_stream = True

            # 3. 如果远程没有，再走官方接口
            if not play_url:
                json_str = await async_req(
                    url2,
                    proxy_addr=self.proxy_addr,
                    headers=self.pc_headers,
                    data=data2
                )
                json_data = json.loads(json_str)
                if 'errorData' in json_data:
                    if json_data['errorData']['code'] == 'needAdult':
                        raise RuntimeError(
                            f"{url} The live room requires login and is only accessible to adults. "
                            f"Please correctly fill in the login cookie in the configuration file."
                        )
                    else:
                        raise RuntimeError(json_data['errorData']['code'], json_data['message'])
                play_url = json_data['PlayList']['hls'][0]['url']

            # 4. 构造播放列表
            if is_m3u_stream:
                play_url_list = [{'quality': 'OD', 'url': play_url}]
            else:
                play_url_list = await self.get_play_url_list(
                    m3u8=play_url,
                    proxy=self.proxy_addr,
                    headers=self.pc_headers
                )

            result |= {
                'is_live': True, 
                'm3u8_url': play_url, 
                'record_url': play_url,
                'play_url_list': play_url_list
            }

        return result

    async def fetch_stream_url(self, json_data: dict, video_quality: str | int | None = None) -> StreamData:
        if json_data.get('m3u8_url') and 'live-video.net/v1/playlist' in json_data.get('m3u8_url', ''):
            direct_url = json_data['m3u8_url']
            data = {
                'platform': 'PandaTV',
                'anchor_name': json_data.get('anchor_name'),
                'is_live': True,
                'title': json_data.get('title'),
                'quality': 'OD',
                'm3u8_url': direct_url,
                'flv_url': None,
                'record_url': direct_url,
                'new_cookies': None,
                'new_token': None,
                'extra': None,
                'live_url': json_data.get('live_url'),
            }
            return wrap_stream(data)

        data = await self.get_stream_url(json_data, video_quality, spec=True, platform='PandaTV')
        return wrap_stream(data)