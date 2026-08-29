"""Shared auto-detect routing for "all" mode and the /all command.

Detects TeraBox, Diskwala, and Flezen links in arbitrary text and dispatches
each to its pipeline. TeraBox links are routed through the user's persisted
TeraBox engine preference (get/exp/exphd), independent of the overall mode.
"""

import asyncio
import logging

from .terabox_trad import process_terabox
from .terabox_exp import process_terabox_experimental
from .diskwala import process_diskwala
from .flezen import process_flezen
from .helpers import extract_all_terabox_url_exp, extract_surl_exp
from diskwalaDL.public_api import extract_all_diskwala_urls
from flezen.public_api import extract_all_flezen_urls
from firebase_db.users import get_user_terabox_mode

log = logging.getLogger(__name__)


async def process_auto(event, text: str) -> bool:
    """Detect and dispatch any TeraBox/Diskwala/Flezen links in `text`.

    Returns True if at least one recognized link was found and dispatched.
    """
    terabox_url_list = extract_all_terabox_url_exp(text)
    diskwala_url_list = extract_all_diskwala_urls(text)
    flezen_url_list = extract_all_flezen_urls(text)

    if not (terabox_url_list or diskwala_url_list or flezen_url_list):
        return False

    tasks = []
    if terabox_url_list:
        terabox_mode = get_user_terabox_mode(event.chat_id)
        if terabox_mode == "get":
            surls = [s for s in (extract_surl_exp(u) for u in terabox_url_list) if s]
            tasks += [process_terabox(event, surl) for surl in surls]
        elif terabox_mode == "exphd":
            tasks += [process_terabox_experimental(event, u, is_hd=True) for u in terabox_url_list]
        else:
            tasks += [process_terabox_experimental(event, u) for u in terabox_url_list]
    tasks += [process_diskwala(event, u) for u in diskwala_url_list]
    tasks += [process_flezen(event, u) for u in flezen_url_list]

    await asyncio.gather(*tasks)
    return True
