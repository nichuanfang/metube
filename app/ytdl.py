import asyncio
import logging
import multiprocessing
import os
import re
import shelve
import subprocess
import time
from collections import OrderedDict
from datetime import datetime
import itertools
import unicodedata

import yt_dlp
from PIL import Image
from mutagen.mp4 import MP4, MP4Cover

from dl_formats import get_format, get_opts, AUDIO_FORMATS

log = logging.getLogger('ytdl')

# needed for sanitizing filenames in restricted mode
ACCENT_CHARS = dict(zip('ÂÃÄÀÁÅÆÇÈÉÊËÌÍÎÏÐÑÒÓÔÕÖŐØŒÙÚÛÜŰÝÞßàáâãäåæçèéêëìíîïðñòóôõöőøœùúûüűýþÿ',
                        itertools.chain('AAAAAA', ['AE'], 'CEEEEIIIIDNOOOOOOO', ['OE'], 'UUUUUY', ['TH', 'ss'],
                                        'aaaaaa', ['ae'], 'ceeeeiiiionooooooo', ['oe'], 'uuuuuy', ['th'], 'y')))
class NO_DEFAULT:
    pass


class DownloadQueueNotifier:
    async def added(self, dl):
        raise NotImplementedError

    async def updated(self, dl):
        raise NotImplementedError

    async def completed(self, dl):
        raise NotImplementedError

    async def canceled(self, id):
        raise NotImplementedError

    async def cleared(self, id):
        raise NotImplementedError

class DownloadInfo:
    def __init__(self, id, title, url, quality, format, folder, custom_name_prefix, error):
        self.id = id if len(custom_name_prefix) == 0 else f'{custom_name_prefix}.{id}'
        self.title = title if len(custom_name_prefix) == 0 else f'{custom_name_prefix}.{title}'
        self.url = url
        self.quality = quality
        self.format = format
        self.folder = folder
        self.custom_name_prefix = custom_name_prefix
        self.msg = self.percent = self.speed = self.eta = None
        self.status = "pending"
        self.size = None
        self.timestamp = time.time_ns()
        self.error = error

class Download:
    manager = None

    def __init__(self, download_dir, temp_dir, output_template, output_template_chapter, quality, format, ytdl_opts, info):
        self.download_dir = download_dir
        self.temp_dir = temp_dir
        self.output_template = output_template
        self.output_template_chapter = output_template_chapter
        self.format = get_format(format, quality)
        self.ytdl_opts = get_opts(format, quality, ytdl_opts)
        self.info = info
        self.canceled = False
        self.tmpfilename = None
        self.status_queue = None
        self.proc = None
        self.loop = None
        self.notifier = None

    @staticmethod
    def _sanitize_filename(s, restricted=False, is_id=NO_DEFAULT):
        """Sanitizes a string so it could be used as part of a filename.
        @param restricted   Use a stricter subset of allowed characters
        @param is_id        Whether this is an ID that should be kept unchanged if possible.
                            If unset, yt-dlp's new sanitization rules are in effect
        """
        if s == '':
            return ''

        def replace_insane(char):
            if restricted and char in ACCENT_CHARS:
                return ACCENT_CHARS[char]
            elif not restricted and char == '\n':
                return '\0 '
            elif is_id is NO_DEFAULT and not restricted and char in '"*:<>?|/\\':
                # Replace with their full-width unicode counterparts
                return {'/': '\u29F8', '\\': '\u29f9'}.get(char, chr(ord(char) + 0xfee0))
            elif char == '?' or ord(char) < 32 or ord(char) == 127:
                return ''
            elif char == '"':
                return '' if restricted else '\''
            elif char == ':':
                return '\0_\0-' if restricted else '\0 \0-'
            elif char in '\\/|*<>':
                return '\0_'
            if restricted and (char in '!&\'()[]{}$;`^,#' or char.isspace() or ord(char) > 127):
                return '' if unicodedata.category(char)[0] in 'CM' else '\0_'
            return char

        # Replace look-alike Unicode glyphs
        if restricted and (is_id is NO_DEFAULT or not is_id):
            s = unicodedata.normalize('NFKC', s)
        s = re.sub(r'[0-9]+(?::[0-9]+)+', lambda m: m.group(0).replace(':', '_'), s)  # Handle timestamps
        result = ''.join(map(replace_insane, s))
        if is_id is NO_DEFAULT:
            result = re.sub(r'(\0.)(?:(?=\1)..)+', r'\1', result)  # Remove repeated substitute chars
            STRIP_RE = r'(?:\0.|[ _-])*'
            result = re.sub(f'^\0.{STRIP_RE}|{STRIP_RE}\0.$', '', result)  # Remove substitute chars from start/end
        result = result.replace('\0', '') or '_'

        if not is_id:
            while '__' in result:
                result = result.replace('__', '_')
            result = result.strip('_')
            # Common case of "Foreign band name - English song title"
            if restricted and result.startswith('-_'):
                result = result[2:]
            if result.startswith('-'):
                result = '_' + result[len('-'):]
            result = result.lstrip('.')
            if not result:
                result = '_'
        return result

    # 定义将图片嵌入到 AAC 文件中的函数
    @staticmethod
    def _embed_thumbnail_in_aac(audio_file, image_file):
        # 临时缩略图文件的路径
        temp_thumbnail = f"{os.path.basename(image_file)[:-4]}-thumbnail.jpg"

        # 使用 PIL 加载图片
        img = Image.open(os.path.abspath(image_file))
        img.thumbnail((300, 300))  # 调整为缩略图大小
        img.save(temp_thumbnail, format="JPEG")  # 保存缩略图为JPEG

        # 重新加载缩略图数据
        with open(temp_thumbnail, "rb") as thumbnail_file:
            thumbnail_data = thumbnail_file.read()

        # 打开 AAC 文件
        audio = MP4(os.path.abspath(audio_file))

        # 添加封面图片
        audio.tags['covr'] = [MP4Cover(thumbnail_data, imageformat=MP4Cover.FORMAT_JPEG)]

        # 保存修改后的 AAC 文件
        audio.save()

        # 删除生成的临时缩略图文件和缩略图
        if os.path.exists(temp_thumbnail):
            os.remove(temp_thumbnail)
        else:
            print(f"文件 {temp_thumbnail} 不存在")
        if os.path.exists(image_file):
            os.remove(image_file)
        else:
            print(f"文件 {image_file} 不存在")

    def _download(self):
        try:
            def put_status(st):
                self.status_queue.put({k: v for k, v in st.items() if k in (
                    'tmpfilename',
                    'filename',
                    'status',
                    'msg',
                    'total_bytes',
                    'total_bytes_estimate',
                    'downloaded_bytes',
                    'speed',
                    'eta',
                )})

            def put_status_postprocessor(d):
                if d['postprocessor'] == 'MoveFiles' and d['status'] == 'finished':
                    if '__finaldir' in d['info_dict']:
                        filename = os.path.join(d['info_dict']['__finaldir'], os.path.basename(d['info_dict']['filepath']))
                    else:
                        filename = d['info_dict']['filepath']
                    self.status_queue.put({'status': 'finished', 'filename': filename})

            ret = yt_dlp.YoutubeDL(params={
                'quiet': True,
                'no_color': True,
                'paths': {"home": self.download_dir, "temp": self.temp_dir},
                'outtmpl': { "default": self.output_template, "chapter": self.output_template_chapter },
                'format': self.format,
                'socket_timeout': 30,
                'ignore_no_formats_error': True,
                'progress_hooks': [put_status],
                'postprocessor_hooks': [put_status_postprocessor],
                **self.ytdl_opts,
            }).download([self.info.url])

            # 如果是m4a格式的 需要使用ffmpeg将flac转为aac格式
            # if self.info.format == "m4a":
            #     sanitized_filename = self._sanitize_filename(self.info.title)
            #     # 获取下载的文件路径
            #     downloaded_file = os.path.join(self.download_dir, f'{sanitized_filename}.flac')
            #     output_file = os.path.join(self.download_dir, f'{sanitized_filename}.m4a')
            #     thumbnail_file = os.path.join(self.download_dir, f'{sanitized_filename}.jpg')
            #     if self.info.quality == 'best':
            #         bitrate = '320'
            #     else:
            #         bitrate = self.info.quality
            #     # 使用ffmpeg进行转换
            #     ffmpeg_cmd = ['ffmpeg', '-i', downloaded_file,"-map", "0:a:0", '-c:a', 'aac', '-b:a', f'{bitrate}k',"-movflags", "faststart", output_file]
            #     subprocess.run(ffmpeg_cmd)
            #     # 嵌入缩略图
            #     self._embed_thumbnail_in_aac(output_file, thumbnail_file)
            #
            #     # 删除原始的 FLAC 文件
            #     if os.path.exists(downloaded_file):
            #         os.remove(downloaded_file)
            #
            #     # 更新队列中的状态，表明已完成转换
            #     self.status_queue.put({'status': 'finished', 'filename': output_file})

            self.status_queue.put({'status': 'finished' if ret == 0 else 'error'})

        except yt_dlp.utils.YoutubeDLError as exc:
            self.status_queue.put({'status': 'error', 'msg': str(exc)})
        except subprocess.CalledProcessError as exc:
            self.status_queue.put({'status': 'error', 'msg': f"ffmpeg error: {str(exc)}"})

    async def start(self, notifier):
        if Download.manager is None:
            Download.manager = multiprocessing.Manager()
        self.status_queue = Download.manager.Queue()
        self.proc = multiprocessing.Process(target=self._download)
        self.proc.start()
        self.loop = asyncio.get_running_loop()
        self.notifier = notifier
        self.info.status = 'preparing'
        await self.notifier.updated(self.info)
        asyncio.create_task(self.update_status())
        return await self.loop.run_in_executor(None, self.proc.join)

    def cancel(self):
        if self.running():
            self.proc.kill()
        self.canceled = True

    def close(self):
        if self.started():
            self.proc.close()
            self.status_queue.put(None)

    def running(self):
        try:
            return self.proc is not None and self.proc.is_alive()
        except ValueError:
            return False

    def started(self):
        return self.proc is not None

    async def update_status(self):
        while True:
            status = await self.loop.run_in_executor(None, self.status_queue.get)
            if status is None:
                return
            self.tmpfilename = status.get('tmpfilename')
            if 'filename' in status:
                fileName = status.get('filename')
                self.info.filename = os.path.relpath(fileName, self.download_dir)
                self.info.size = os.path.getsize(fileName) if os.path.exists(fileName) else None

                # Set correct file extension for thumbnails
                if(self.info.format == 'thumbnail'):
                    self.info.filename = re.sub(r'\.webm$', '.jpg', self.info.filename)
            self.info.status = status['status']
            self.info.msg = status.get('msg')
            if 'downloaded_bytes' in status:
                total = status.get('total_bytes') or status.get('total_bytes_estimate')
                if total:
                    self.info.percent = status['downloaded_bytes'] / total * 100
            self.info.speed = status.get('speed')
            self.info.eta = status.get('eta')
            await self.notifier.updated(self.info)

class PersistentQueue:
    def __init__(self, path):
        pdir = os.path.dirname(path)
        if not os.path.isdir(pdir):
            os.mkdir(pdir)
        with shelve.open(path, 'c'):
            pass
        self.path = path
        self.dict = OrderedDict()

    def load(self):
        for k, v in self.saved_items():
            self.dict[k] = Download(None, None, None, None, None, None, {}, v)

    def exists(self, key):
        return key in self.dict

    def get(self, key):
        return self.dict[key]

    def items(self):
        return self.dict.items()

    def saved_items(self):
        with shelve.open(self.path, 'r') as shelf:
            return sorted(shelf.items(), key=lambda item: item[1].timestamp)

    def put(self, value):
        key = value.info.url
        self.dict[key] = value
        with shelve.open(self.path, 'w') as shelf:
            shelf[key] = value.info

    def delete(self, key):
        del self.dict[key]
        with shelve.open(self.path, 'w') as shelf:
            shelf.pop(key)

    def next(self):
        k, v = next(iter(self.dict.items()))
        return k, v

    def empty(self):
        return not bool(self.dict)

class DownloadQueue:
    def __init__(self, config, notifier):
        self.config = config
        self.notifier = notifier
        self.queue = PersistentQueue(self.config.STATE_DIR + '/queue')
        self.done = PersistentQueue(self.config.STATE_DIR + '/completed')
        self.pending = PersistentQueue(self.config.STATE_DIR + '/pending')
        self.done.load()

    async def __import_queue(self):
        for k, v in self.queue.saved_items():
            await self.add(v.url, v.quality, v.format, v.folder, v.custom_name_prefix, True, 0)

    async def initialize(self):
        self.event = asyncio.Event()
        asyncio.create_task(self.__download())
        asyncio.create_task(self.__import_queue())

    def __extract_info(self, url, playlist_strict_mode):
        return yt_dlp.YoutubeDL(params={
            'quiet': True,
            'no_color': True,
            'extract_flat': True,
            'ignore_no_formats_error': True,
            'noplaylist': playlist_strict_mode,
            'paths': {"home": self.config.DOWNLOAD_DIR, "temp": self.config.TEMP_DIR},
            **self.config.YTDL_OPTIONS,
        }).extract_info(url, download=False)

    def __calc_download_path(self, quality, format, folder):
        """Calculates download path from quality, format and folder attributes.

        Returns:
            Tuple dldirectory, error_message both of which might be None (but not at the same time)
        """
        # Keep consistent with frontend
        base_directory = self.config.DOWNLOAD_DIR if (quality != 'audio' and format not in AUDIO_FORMATS) else self.config.AUDIO_DOWNLOAD_DIR
        if folder:
            if not self.config.CUSTOM_DIRS:
                return None, {'status': 'error', 'msg': f'A folder for the download was specified but CUSTOM_DIRS is not true in the configuration.'}
            dldirectory = os.path.realpath(os.path.join(base_directory, folder))
            real_base_directory = os.path.realpath(base_directory)
            if not dldirectory.startswith(real_base_directory):
                return None, {'status': 'error', 'msg': f'Folder "{folder}" must resolve inside the base download directory "{real_base_directory}"'}
            if not os.path.isdir(dldirectory):
                if not self.config.CREATE_CUSTOM_DIRS:
                    return None, {'status': 'error', 'msg': f'Folder "{folder}" for download does not exist inside base directory "{real_base_directory}", and CREATE_CUSTOM_DIRS is not true in the configuration.'}
                os.makedirs(dldirectory, exist_ok=True)
        else:
            dldirectory = base_directory
        return dldirectory, None

    async def __add_entry(self, entry, quality, format, folder, custom_name_prefix, playlist_strict_mode, playlist_item_limit, auto_start, already):
        if not entry:
            return {'status': 'error', 'msg': "Invalid/empty data was given."}

        error = None
        if "live_status" in entry and "release_timestamp" in entry and entry.get("live_status") == "is_upcoming":
            dt_ts = datetime.fromtimestamp(entry.get("release_timestamp")).strftime('%Y-%m-%d %H:%M:%S %z')
            error = f"Live stream is scheduled to start at {dt_ts}"
        else:
            if "msg" in entry:
                error = entry["msg"]

        etype = entry.get('_type') or 'video'

        if etype.startswith('url'):
            log.debug('Processing as an url')
            return await self.add(entry['url'], quality, format, folder, custom_name_prefix, playlist_strict_mode, playlist_item_limit, auto_start, already)
        elif etype == 'playlist':
            log.debug('Processing as a playlist')
            entries = entry['entries']
            log.info(f'playlist detected with {len(entries)} entries')
            playlist_index_digits = len(str(len(entries)))
            results = []
            if playlist_item_limit > 0:
                log.info(f'Playlist item limit is set. Processing only first {playlist_item_limit} entries')
                entries = entries[:playlist_item_limit]
            for index, etr in enumerate(entries, start=1):
                etr["_type"] = "video" # Prevents video to be treated as url and lose below properties during processing
                etr["playlist"] = entry["id"]
                etr["playlist_index"] = '{{0:0{0:d}d}}'.format(playlist_index_digits).format(index)
                for property in ("id", "title", "uploader", "uploader_id"):
                    if property in entry:
                        etr[f"playlist_{property}"] = entry[property]
                results.append(await self.__add_entry(etr, quality, format, folder, custom_name_prefix, playlist_strict_mode, playlist_item_limit, auto_start, already))
            if any(res['status'] == 'error' for res in results):
                return {'status': 'error', 'msg': ', '.join(res['msg'] for res in results if res['status'] == 'error' and 'msg' in res)}
            return {'status': 'ok'}
        elif etype == 'video' or etype.startswith('url') and 'id' in entry and 'title' in entry:
            log.debug('Processing as a video')
            if not self.queue.exists(entry['id']):
                dl = DownloadInfo(entry['id'], entry.get('title') or entry['id'], entry.get('webpage_url') or entry['url'], quality, format, folder, custom_name_prefix, error)
                dldirectory, error_message = self.__calc_download_path(quality, format, folder)
                if error_message is not None:
                    return error_message
                output = self.config.OUTPUT_TEMPLATE if len(custom_name_prefix) == 0 else f'{custom_name_prefix}.{self.config.OUTPUT_TEMPLATE}'
                output_chapter = self.config.OUTPUT_TEMPLATE_CHAPTER
                if 'playlist' in entry and entry['playlist'] is not None:
                    if len(self.config.OUTPUT_TEMPLATE_PLAYLIST):
                        output = self.config.OUTPUT_TEMPLATE_PLAYLIST

                    for property, value in entry.items():
                        if property.startswith("playlist"):
                            output = output.replace(f"%({property})s", str(value))

                ytdl_options = dict(self.config.YTDL_OPTIONS)

                if playlist_item_limit > 0:
                    log.info(f'playlist limit is set. Processing only first {playlist_item_limit} entries')
                    ytdl_options['playlistend'] = playlist_item_limit

                if auto_start is True:
                    self.queue.put(Download(dldirectory, self.config.TEMP_DIR, output, output_chapter, quality, format, ytdl_options, dl))
                    self.event.set()
                else:
                    self.pending.put(Download(dldirectory, self.config.TEMP_DIR, output, output_chapter, quality, format, ytdl_options, dl))
                await self.notifier.added(dl)
            return {'status': 'ok'}
        return {'status': 'error', 'msg': f'Unsupported resource "{etype}"'}

    async def add(self, url, quality, format, folder, custom_name_prefix, playlist_strict_mode, playlist_item_limit, auto_start=True, already=None):
        log.info(f'adding {url}: {quality=} {format=} {already=} {folder=} {custom_name_prefix=} {playlist_strict_mode=} {playlist_item_limit=}')
        already = set() if already is None else already
        if url in already:
            log.info('recursion detected, skipping')
            return {'status': 'ok'}
        else:
            already.add(url)
        try:
            entry = await asyncio.get_running_loop().run_in_executor(None, self.__extract_info, url, playlist_strict_mode)
        except yt_dlp.utils.YoutubeDLError as exc:
            return {'status': 'error', 'msg': str(exc)}
        return await self.__add_entry(entry, quality, format, folder, custom_name_prefix, playlist_strict_mode, playlist_item_limit, auto_start, already)

    async def start_pending(self, ids):
        for id in ids:
            if not self.pending.exists(id):
                log.warn(f'requested start for non-existent download {id}')
                continue
            dl = self.pending.get(id)
            self.queue.put(dl)
            self.pending.delete(id)
            self.event.set()
        return {'status': 'ok'}

    async def cancel(self, ids):
        for id in ids:
            if self.pending.exists(id):
                self.pending.delete(id)
                await self.notifier.canceled(id)
                continue
            if not self.queue.exists(id):
                log.warn(f'requested cancel for non-existent download {id}')
                continue
            if self.queue.get(id).started():
                self.queue.get(id).cancel()
            else:
                self.queue.delete(id)
                await self.notifier.canceled(id)
        return {'status': 'ok'}

    async def clear(self, ids):
        for id in ids:
            if not self.done.exists(id):
                log.warn(f'requested delete for non-existent download {id}')
                continue
            if self.config.DELETE_FILE_ON_TRASHCAN:
                dl = self.done.get(id)
                try:
                    dldirectory, _ = self.__calc_download_path(dl.info.quality, dl.info.format, dl.info.folder)
                    os.remove(os.path.join(dldirectory, dl.info.filename))
                except Exception as e:
                    log.warn(f'deleting file for download {id} failed with error message {e!r}')
            self.done.delete(id)
            await self.notifier.cleared(id)
        return {'status': 'ok'}

    def get(self):
        return(list((k, v.info) for k, v in self.queue.items()) + list((k, v.info) for k, v in self.pending.items()),
               list((k, v.info) for k, v in self.done.items()))

    async def __download(self):
        while True:
            while self.queue.empty():
                log.info('waiting for item to download')
                await self.event.wait()
                self.event.clear()
            id, entry = self.queue.next()
            log.info(f'downloading {entry.info.title}')
            await entry.start(self.notifier)
            if entry.info.status != 'finished':
                if entry.tmpfilename and os.path.isfile(entry.tmpfilename):
                    try:
                        os.remove(entry.tmpfilename)
                    except:
                        pass
                entry.info.status = 'error'
            entry.close()
            if self.queue.exists(id):
                self.queue.delete(id)
                if entry.canceled:
                    await self.notifier.canceled(id)
                else:
                    self.done.put(entry)
                    await self.notifier.completed(entry.info)
