"""Bounded YouTube launch input; URLs never become persistent app settings."""
import re
from urllib.parse import parse_qs, urlencode, urlsplit


WATCH_LATER_URL = 'https://www.youtube.com/playlist?list=WL'
VIDEO_ID = re.compile(r'[A-Za-z0-9_-]{11}')
HOSTS = {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be', 'www.youtu.be'}


def _start_seconds(value):
    if re.fullmatch(r'[0-9]{1,7}', value):
        seconds = int(value)
    else:
        match = re.fullmatch(r'(?:(\d{1,4})h)?(?:(\d{1,5})m)?(?:(\d{1,7})s)?', value)
        if not match or not any(match.groups()):
            raise ValueError('Invalid YouTube start time')
        hours, minutes, seconds = (int(part or 0) for part in match.groups())
        seconds += hours * 3600 + minutes * 60
    if seconds > 604800:
        raise ValueError('Invalid YouTube start time')
    return seconds


def video_url(value):
    """Normalize supported video links and keep only an explicit start time."""
    if not isinstance(value, str) or not 1 <= len(value) <= 4096 or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Enter a YouTube video URL')
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {'http', 'https'} or parsed.hostname not in HOSTS or parsed.username is not None or parsed.password is not None:
            raise ValueError()
        if parsed.port is not None and parsed.port != (443 if parsed.scheme == 'https' else 80):
            raise ValueError()
        query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=64)
        if parsed.hostname in {'youtu.be', 'www.youtu.be'}:
            candidate = parsed.path.removeprefix('/')
        elif parsed.path == '/watch':
            if len(query.get('v', [])) != 1:
                raise ValueError()
            candidate = query['v'][0]
        else:
            match = re.fullmatch(r'/(?:shorts|live|embed)/([A-Za-z0-9_-]{11})/?', parsed.path)
            if not match:
                raise ValueError()
            candidate = match[1]
        if not VIDEO_ID.fullmatch(candidate):
            raise ValueError()
        starts = []
        for key in ('t', 'start'):
            if key in query:
                if len(query[key]) != 1:
                    raise ValueError()
                starts.append(_start_seconds(query[key][0]))
        if parsed.fragment.startswith('t='):
            starts.append(_start_seconds(parsed.fragment[2:]))
        if len(set(starts)) > 1:
            raise ValueError()
        normalized = {'v': candidate}
        if starts and starts[0]:
            normalized['t'] = str(starts[0]) + 's'
        return 'https://www.youtube.com/watch?' + urlencode(normalized)
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Enter a supported YouTube video URL and start time') from None


def launch(mode, *, url=None, resume=True):
    if type(resume) is not bool:
        raise ValueError('Resume must be true or false')
    if mode == 'video':
        return {'mode': mode, 'url': video_url(url), 'resume': resume}
    if mode == 'watch_later' and url is None:
        return {'mode': mode, 'resume': resume}
    raise ValueError('Choose a YouTube video or Watch Later')
