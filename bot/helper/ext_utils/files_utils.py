import os
import json

from os import path as ospath, walk
from shutil import rmtree, disk_usage
from sys import exit as sexit
from subprocess import run as srun
from re import sub as re_sub, search as re_search, split as re_split, I
from hashlib import md5
from time import strftime, gmtime, time
from shlex import split as ssplit
from aiofiles.os import remove as aioremove, path as aiopath, mkdir, makedirs, listdir, rmdir
from aioshutil import rmtree as aiormtree
from asyncio import create_subprocess_exec, create_task, gather
from asyncio.subprocess import PIPE
from telegraph import upload_file
from langcodes import Language
from natsort import natsorted
from magic import Magic

from bot import LOGGER, MAX_SPLIT_SIZE, config_dict, user_data, aria2, get_client, GLOBAL_EXTENSION_FILTER
from bot.modules.mediainfo import parseinfo
from bot.helper.ext_utils.bot_utils import cmd_exec, sync_to_async, get_readable_file_size, get_readable_time
from bot.helper.ext_utils.telegraph_helper import telegraph
from .exceptions import NotSupportedExtractionArchive

FIRST_SPLIT_REGEX = r'(\.|_)part0*1\.rar$|(\.|_)7z\.0*1$|(\.|_)zip\.0*1$|^(?!.*(\.|_)part\d+\.rar$).*\.rar$'
SPLIT_REGEX = r'\.r\d+$|\.7z\.\d+$|\.z\d+$|\.zip\.\d+$'
ARCH_EXT = [
    ".tar.bz2",
    ".tar.gz",
    ".bz2",
    ".gz",
    ".tar.xz",
    ".tar",
    ".tbz2",
    ".tgz",
    ".lzma2",
    ".zip",
    ".7z",
    ".z",
    ".rar",
    ".iso",
    ".wim",
    ".cab",
    ".apm",
    ".arj",
    ".chm",
    ".cpio",
    ".cramfs",
    ".deb",
    ".dmg",
    ".fat",
    ".hfs",
    ".lzh",
    ".lzma",
    ".mbr",
    ".msi",
    ".mslz",
    ".nsis",
    ".ntfs",
    ".rpm",
    ".squashfs",
    ".udf",
    ".vhd",
    ".xar"
]


async def is_multi_streams(path):
    try:
        result = await cmd_exec(["ffprobe", "-hide_banner", "-loglevel", "error", "-print_format", "json", "-show_streams", path])
        if res := result[1]:
            LOGGER.warning(f'Get Video Streams: {res}')
    except Exception as e:
        LOGGER.error(f'Get Video Streams: {e}. Mostly File not found!')
        return False
    fields = eval(result[0]).get('streams')
    if fields is None:
        LOGGER.error(f"get_video_streams: {result}")
        return False
    videos = 0
    audios = 0
    for stream in fields:
        if stream.get('codec_type') == 'video':
            videos += 1
        elif stream.get('codec_type') == 'audio':
            audios += 1
    return videos > 1 or audios > 1


async def get_media_info(path, metadata=False):
    try:
        result = await cmd_exec(["ffprobe", "-hide_banner", "-loglevel", "error", "-print_format", "json", "-show_format", "-show_streams", path])
        if res := result[1]:
            LOGGER.warning(f'Get Media Info: {res}')
    except Exception as e:
        LOGGER.error(f'Media Info: {e}. Mostly File not found!')
        return (0, "", "", "") if metadata else (0, None, None)
    ffresult = eval(result[0])
    fields = ffresult.get('format')
    if fields is None:
        LOGGER.error(f"Media Info Sections: {result}")
        return (0, "", "", "") if metadata else (0, None, None)
    duration = round(float(fields.get('duration', 0)))
    if metadata:
        lang, qual, stitles = "", "", ""
        if (streams := ffresult.get('streams')) and streams[0].get('codec_type') == 'video':
            qual = int(streams[0].get('height'))
            qual = f"{480 if qual <= 480 else 540 if qual <= 540 else 720 if qual <= 720 else 1080 if qual <= 1080 else 2160 if qual <= 2160 else 4320 if qual <= 4320 else 8640}p"
            for stream in streams:
                if stream.get('codec_type') == 'audio' and (lc := stream.get('tags', {}).get('language')):
                    try:
                        lc = Language.get(lc).display_name()
                        if lc not in lang:
                            lang += f"{lc}, "
                    except Exception:
                    	  pass
                if stream.get('codec_type') == 'subtitle' and (st := stream.get('tags', {}).get('language')):
                    try:
                        st = Language.get(st).display_name()
                        if st not in stitles:
                            stitles += f"{st}, "
                    except Exception:
                        pass
                    
        return duration, qual, lang[:-2], stitles[:-2]
    tags = fields.get('tags', {})
    artist = tags.get('artist') or tags.get('ARTIST') or tags.get("Artist")
    title = tags.get('title') or tags.get('TITLE') or tags.get("Title")
    return duration, artist, title


async def get_document_type(path):
    is_video, is_audio, is_image = False, False, False
    if path.endswith(tuple(ARCH_EXT)) or re_search(r'.+(\.|_)(rar|7z|zip|bin)(\.0*\d+)?$', path):
        return is_video, is_audio, is_image
    mime_type = await sync_to_async(get_mime_type, path)
    if mime_type.startswith('audio'):
        return False, True, False
    if mime_type.startswith('image'):
        return False, False, True
    if not mime_type.startswith('video') and not mime_type.endswith('octet-stream'):
        return is_video, is_audio, is_image
    try:
        result = await cmd_exec(["ffprobe", "-hide_banner", "-loglevel", "error", "-print_format", "json", "-show_streams", path])
        if res := result[1]:
            LOGGER.warning(f'Get Document Type: {res}')
    except Exception as e:
        LOGGER.error(f'Get Document Type: {e}. Mostly File not found!')
        return is_video, is_audio, is_image
    fields = eval(result[0]).get('streams')
    if fields is None:
        LOGGER.error(f"get_document_type: {result}")
        return is_video, is_audio, is_image
    for stream in fields:
        if stream.get('codec_type') == 'video':
            is_video = True
        elif stream.get('codec_type') == 'audio':
            is_audio = True
    return is_video, is_audio, is_image


async def get_audio_thumb(audio_file):
    des_dir = 'Thumbnails'
    if not await aiopath.exists(des_dir):
        await mkdir(des_dir)
    des_dir = ospath.join(des_dir, f"{time()}.jpg")
    cmd = ["render", "-hide_banner", "-loglevel", "error", "-i", audio_file, "-an", "-vcodec", "copy", des_dir]
    status = await create_subprocess_exec(*cmd, stderr=PIPE)
    if await status.wait() != 0 or not await aiopath.exists(des_dir):
        err = (await status.stderr.read()).decode().strip()
        LOGGER.error(f'Error while extracting thumbnail from audio. Name: {audio_file} stderr: {err}')
        return None
    return des_dir


async def take_ss(video_file, duration=None, total=1, gen_ss=False):
    des_dir = ospath.join('Thumbnails', f"{time()}")
    await makedirs(des_dir, exist_ok=True)
    if duration is None:
        duration = (await get_media_info(video_file))[0]
    if duration == 0:
        duration = 3
    duration = duration - (duration * 2 / 100)
    cmd = ["render", "-hide_banner", "-loglevel", "error", "-ss", "", "-i", video_file, "-vf", "thumbnail", "-frames:v", "1", des_dir]
    tasks = []
    tstamps = {}
    for eq_thumb in range(1, total+1):
        cmd[5] = str((duration // total) * eq_thumb)
        tstamps[f"aeon_{eq_thumb}.jpg"] = strftime("%H:%M:%S", gmtime(float(cmd[5])))
        cmd[-1] = ospath.join(des_dir, f"aeon_{eq_thumb}.jpg")
        tasks.append(create_task(create_subprocess_exec(*cmd, stderr=PIPE)))
    status = await gather(*tasks)
    for task, eq_thumb in zip(status, range(1, total+1)):
        if await task.wait() != 0 or not await aiopath.exists(ospath.join(des_dir, f"aeon_{eq_thumb}.jpg")):
            err = (await task.stderr.read()).decode().strip()
            LOGGER.error(f'Error while extracting thumbnail no. {eq_thumb} from video. Name: {video_file} stderr: {err}')
            await aiormtree(des_dir)
            return None
    return (des_dir, tstamps) if gen_ss else ospath.join(des_dir, "aeon_1.jpg")


async def split_file(path, size, file_, dirpath, split_size, listener, start_time=0, i=1, inLoop=False, multi_streams=True):
    if listener.suproc == 'cancelled' or listener.suproc is not None and listener.suproc.returncode == -9:
        return False
    if listener.seed and not listener.newDir:
        dirpath = f"{dirpath}/splited_files"
        if not await aiopath.exists(dirpath):
            await mkdir(dirpath)
    user_id = listener.message.from_user.id
    user_dict = user_data.get(user_id, {})
    leech_split_size = MAX_SPLIT_SIZE
    parts = -(-size // leech_split_size)
    if (await get_document_type(path))[0]:
        if multi_streams:
            multi_streams = await is_multi_streams(path)
        duration = (await get_media_info(path))[0]
        base_name, extension = ospath.splitext(file_)
        split_size -= 5000000
        while i <= parts or start_time < duration - 4:
            parted_name = f"{base_name}.part{i:03}{extension}"
            out_path = ospath.join(dirpath, parted_name)
            cmd = ["render", "-hide_banner", "-loglevel", "error", "-ss", str(start_time), "-i", path, "-fs", str(split_size), "-map", "0", "-map_chapters", "-1", "-async", "1", "-strict", "-2", "-c", "copy", out_path]
            if not multi_streams:
                del cmd[10]
                del cmd[10]
            if listener.suproc == 'cancelled' or listener.suproc is not None and listener.suproc.returncode == -9:
                return False
            listener.suproc = await create_subprocess_exec(*cmd, stderr=PIPE)
            code = await listener.suproc.wait()
            if code == -9:
                return False
            elif code != 0:
                err = (await listener.suproc.stderr.read()).decode().strip()
                try:
                    await aioremove(out_path)
                except:
                    pass
                if multi_streams:
                    LOGGER.warning(
                        f"{err}. Retrying without map, -map 0 not working in all situations. Path: {path}")
                    return await split_file(path, size, file_, dirpath, split_size, listener, start_time, i, True, False)
                else:
                    LOGGER.warning(f"{err}. Unable to split this video, if it's size less than {MAX_SPLIT_SIZE} will be uploaded as it is. Path: {path}")
                return "errored"
            out_size = await aiopath.getsize(out_path)
            if out_size > MAX_SPLIT_SIZE:
                dif = out_size - MAX_SPLIT_SIZE
                split_size -= dif + 5000000
                await aioremove(out_path)
                return await split_file(path, size, file_, dirpath, split_size, listener, start_time, i, True, )
            lpd = (await get_media_info(out_path))[0]
            if lpd == 0:
                LOGGER.error(f'Something went wrong while splitting, mostly file is corrupted. Path: {path}')
                break
            elif duration == lpd:
                LOGGER.warning(f"This file has been splitted with default stream and audio, so you will only see one part with less size from orginal one because it doesn't have all streams and audios. This happens mostly with MKV videos. Path: {path}")
                break
            elif lpd <= 3:
                await aioremove(out_path)
                break
            start_time += lpd - 3
            i += 1
    else:
        out_path = ospath.join(dirpath, f"{file_}.")
        listener.suproc = await create_subprocess_exec("split", "--numeric-suffixes=1", "--suffix-length=3", f"--bytes={split_size}", path, out_path, stderr=PIPE)
        code = await listener.suproc.wait()
        if code == -9:
            return False
        elif code != 0:
            err = (await listener.suproc.stderr.read()).decode().strip()
            LOGGER.error(err)
    return True

async def format_filename(file_, user_id, dirpath=None, isMirror=False):
    user_dict = user_data.get(user_id, {})
    prefix = user_dict.get('prefix', '')
    remname = user_dict.get('remname', '')
    suffix = user_dict.get('suffix', '')
    lcaption = user_dict.get('lcaption', '')
    metadata = user_dict.get('metadata', '')
    prefile_ = file_
    file_ = re_sub(r'www\S+', '', file_)

    if metadata and dirpath and file_.lower().endswith('.mkv'):
        file_ = await change_metadata(file_, dirpath, metadata)
  
    if remname:
        if not remname.startswith('|'):
            remname = f"|{remname}"
        remname = remname.replace('\s', ' ')
        slit = remname.split("|")
        __newFileName = ospath.splitext(file_)[0]
        for rep in range(1, len(slit)):
            args = slit[rep].split(":")
            if len(args) == 3:
                __newFileName = re_sub(args[0], args[1], __newFileName, int(args[2]))
            elif len(args) == 2:
                __newFileName = re_sub(args[0], args[1], __newFileName)
            elif len(args) == 1:
                __newFileName = re_sub(args[0], '', __newFileName)
        file_ = __newFileName + ospath.splitext(file_)[1]
        LOGGER.info(f"New Filename : {file_}")

    nfile_ = file_
    if prefix:
        nfile_ = prefix.replace('\s', ' ') + file_
        prefix = re_sub(r'<.*?>', '', prefix).replace('\s', ' ')
        if not file_.startswith(prefix):
            file_ = f"{prefix}{file_}"

    if suffix and not isMirror:
        suffix = suffix.replace('\s', ' ')
        sufLen = len(suffix)
        fileDict = file_.split('.')
        _extIn = 1 + len(fileDict[-1])
        _extOutName = '.'.join(fileDict[:-1]).replace('.', ' ').replace('-', ' ')
        _newExtFileName = f"{_extOutName}{suffix}.{fileDict[-1]}"
        if len(_extOutName) > (64 - (sufLen + _extIn)):
            _newExtFileName = (_extOutName[:64 - (sufLen + _extIn)] + f"{suffix}.{fileDict[-1]}")
        file_ = _newExtFileName
    elif suffix:
        suffix = suffix.replace('\s', ' ')
        file_ = f"{ospath.splitext(file_)[0]}{suffix}{ospath.splitext(file_)[1]}" if '.' in file_ else f"{file_}{suffix}"

    cap_mono = nfile_
    if lcaption and dirpath and not isMirror:
        def lowerVars(match):
            return f"{{{match.group(1).lower()}}}"
        lcaption = lcaption.replace('\|', '%%').replace('\{', '&%&').replace('\}', '$%$').replace('\s', ' ')
        slit = lcaption.split("|")
        slit[0] = re_sub(r'\{([^}]+)\}', lowerVars, slit[0])
        up_path = ospath.join(dirpath, prefile_)
        dur, qual, lang, subs = await get_media_info(up_path, True)
        cap_mono = slit[0].format(
            filename=nfile_,
            size=get_readable_file_size(await aiopath.getsize(up_path)),
            duration=get_readable_time(dur, True),
            quality=qual,
            languages=lang,
            subtitles=subs,
            md5_hash=get_md5_hash(up_path))
        if len(slit) > 1:
            for rep in range(1, len(slit)):
                args = slit[rep].split(":")
                if len(args) == 3:
                    cap_mono = cap_mono.replace(args[0], args[1], int(args[2]))
                elif len(args) == 2:
                    cap_mono = cap_mono.replace(args[0], args[1])
                elif len(args) == 1:
                    cap_mono = cap_mono.replace(args[0], '')
        cap_mono = cap_mono.replace('%%', '|').replace('&%&', '{').replace('$%$', '}')
    return file_, cap_mono


async def get_ss(up_path, ss_no):
    thumbs_path, tstamps = await take_ss(up_path, total=ss_no, gen_ss=True)
    th_html = f"<h4>{ospath.basename(up_path)}</h4><br><b>Total Screenshots:</b> {ss_no}<br><br>"
    th_html += ''.join(f'<img src="https://graph.org{upload_file(ospath.join(thumbs_path, thumb))[0]}"><br><pre>Screenshot at {tstamps[thumb]}</pre>' for thumb in natsorted(await listdir(thumbs_path)))
    await aiormtree(thumbs_path)
    link_id = (await telegraph.create_page(title="ScreenShots", content=th_html))["path"]
    return f"https://graph.org/{link_id}"


async def get_mediainfo_link(up_path):
    stdout, __, _ = await cmd_exec(ssplit(f'mediainfo "{up_path}"'))
    tc = f"<h4>{ospath.basename(up_path)}</h4><br><br>"
    if len(stdout) != 0:
        tc += parseinfo(stdout)
    link_id = (await telegraph.create_page(title="MediaInfo", content=tc))["path"]
    return f"https://graph.org/{link_id}"


def get_md5_hash(up_path):
    md5_hash = md5()
    with open(up_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            md5_hash.update(byte_block)
        return md5_hash.hexdigest()


async def change_metadata(file, dirpath, key):
    LOGGER.info(f"Trying to change metadata for file: {file}")
    temp_file = f"{file}.temp.mkv"
    
    full_file_path = os.path.join(dirpath, file)
    temp_file_path = os.path.join(dirpath, temp_file)
    
    cmd = ['ffprobe', '-hide_banner', '-loglevel', 'error', '-print_format', 'json', '-show_streams', '-show_format', full_file_path]
    process = await create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
    stdout, stderr = await process.communicate()
    
    if process.returncode != 0:
        LOGGER.error(f"Error getting stream info: {stderr.decode().strip()}")
        return file
    
    metadata = json.loads(stdout)
    streams = metadata['streams']
    format_metadata = metadata['format'].get('tags', {})
    
    cmd = ['render', '-y', '-i', full_file_path, '-c', 'copy', '-metadata', f'title={key}']
    
    unset_metadata_keys = ['LICENCE', 'author', 'description', 'Description', 'AUTHOR', 'filename', 'mimetype', 'SUMMARY', 'WEBSITE', 'COMMENT', 'ENCODER', 'Copyright']
    for unset_key in unset_metadata_keys:
        if unset_key in format_metadata:
            cmd.extend(['-metadata', f'{unset_key}='])
    
    audio_index = 0
    subtitle_index = 0
    
    for stream in streams:
        stream_index = stream['index']
        stream_type = stream['codec_type']
        
        cmd.extend(['-map', f'0:{stream_index}'])
        
        if stream_type == 'video':
            cmd.extend([f'-metadata:s:v:{stream_index}', f'title={key}'])
        elif stream_type == 'audio':
            cmd.extend([f'-metadata:s:a:{audio_index}', f'title={key}'])
            audio_index += 1
        elif stream_type == 'subtitle':
            cmd.extend([f'-metadata:s:s:{subtitle_index}', f'title={key}'])
            subtitle_index += 1
        
        for unset_key in unset_metadata_keys:
            if 'tags' in stream and unset_key in stream['tags']:
                cmd.extend([f'-metadata:s:{stream_index}:{unset_key}=', ''])
    
    cmd.append(temp_file_path)
    
    process = await create_subprocess_exec(*cmd, stderr=PIPE)
    await process.wait()
    
    if process.returncode != 0:
        err = (await process.stderr.read()).decode().strip()
        LOGGER.error(err)
        LOGGER.error(f"Error changing metadata for file: {file}")
        return file
    
    os.replace(temp_file_path, full_file_path)
    LOGGER.info(f"Metadata changed successfully for file: {file}")
    return file

def is_first_archive_split(file):
    return bool(re_search(FIRST_SPLIT_REGEX, file))


def is_archive(file):
    return file.endswith(tuple(ARCH_EXT))


def is_archive_split(file):
    return bool(re_search(SPLIT_REGEX, file))


async def clean_target(path):
    if await aiopath.exists(path):
        LOGGER.info(f"Cleaning Target: {path}")
        if await aiopath.isdir(path):
            try:
                await aiormtree(path)
            except:
                pass
        elif await aiopath.isfile(path):
            try:
                await aioremove(path)
            except:
                pass


async def clean_download(path):
    if await aiopath.exists(path):
        LOGGER.info(f"Cleaning Download: {path}")
        try:
            await aiormtree(path)
        except:
            pass


async def start_cleanup():
    get_client().torrents_delete(torrent_hashes="all")
    try:
        await aiormtree('/usr/src/app/downloads/')
    except:
        pass
    await makedirs('/usr/src/app/downloads/', exist_ok = True)


def clean_all():
    aria2.remove_all(True)
    get_client().torrents_delete(torrent_hashes="all")
    try:
        rmtree('/usr/src/app/downloads/')
    except:
        pass


def exit_clean_up(signal, frame):
    try:
        LOGGER.info("Please wait, while we clean up and stop the running downloads")
        clean_all()
        srun(['pkill', '-9', '-f', '-e','gunicorn|buffet|openstack|render|zcl'])
        sexit(0)
    except KeyboardInterrupt:
        LOGGER.warning("Force Exiting before the cleanup finishes!")
        sexit(1)


async def clean_unwanted(path):
    LOGGER.info(f"Cleaning unwanted files/folders: {path}")
    for dirpath, _, files in await sync_to_async(walk, path, topdown=False):
        for filee in files:
            if filee.endswith(".!qB") or filee.endswith('.parts') and filee.startswith('.'):
                await aioremove(ospath.join(dirpath, filee))
        if dirpath.endswith((".unwanted", "splited_files", "copied")):
            await aiormtree(dirpath)
    for dirpath, _, files in await sync_to_async(walk, path, topdown=False):
        if not await listdir(dirpath):
            await rmdir(dirpath)


async def get_path_size(path):
    if await aiopath.isfile(path):
        return await aiopath.getsize(path)
    total_size = 0
    for root, dirs, files in await sync_to_async(walk, path):
        for f in files:
            abs_path = ospath.join(root, f)
            total_size += await aiopath.getsize(abs_path)
    return total_size


async def count_files_and_folders(path):
    total_files = 0
    total_folders = 0
    for _, dirs, files in await sync_to_async(walk, path):
        total_files += len(files)
        for f in files:
            if f.endswith(tuple(GLOBAL_EXTENSION_FILTER)):
                total_files -= 1
        total_folders += len(dirs)
    return total_folders, total_files


def get_base_name(orig_path):
    extension = next(
        (ext for ext in ARCH_EXT if orig_path.lower().endswith(ext)), ''
    )
    if extension != '':
        return re_split(f'{extension}$', orig_path, maxsplit=1, flags=I)[0]
    else:
        raise NotSupportedExtractionArchive(
            'File format not supported for extraction')


def get_mime_type(file_path):
    mime = Magic(mime=True)
    mime_type = mime.from_file(file_path)
    mime_type = mime_type or "text/plain"
    return mime_type


def check_storage_threshold(size, threshold, arch=False, alloc=False):
    free = disk_usage('/usr/src/app/downloads/').free
    if not alloc:
        if (not arch and free - size < threshold or arch and free - (size * 2) < threshold):
            return False
    elif not arch:
        if free < threshold:
            return False
    elif free - size < threshold:
        return False
    return True


async def join_files(path):
    files = await listdir(path)
    results = []
    for file_ in files:
        if re_search(r"\.0+2$", file_) and await sync_to_async(get_mime_type, f'{path}/{file_}') == 'application/octet-stream':
            final_name = file_.rsplit('.', 1)[0]
            cmd = f'cat {path}/{final_name}.* > {path}/{final_name}'
            _, stderr, code = await cmd_exec(cmd, True)
            if code != 0:
                LOGGER.error(f'Failed to join {final_name}, stderr: {stderr}')
            else:
                results.append(final_name)
    if results:
        for res in results:
            for file_ in files:
                if re_search(fr"{res}\.0[0-9]+$", file_):
                    await aioremove(f'{path}/{file_}')
