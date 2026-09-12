import os
import asyncio
from pathlib import Path
import shutil
import subprocess

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from b2sdk.v2 import B2Api, InMemoryAccountInfo


# ============================================================
# CONFIGURATION
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "8769661029:AAED5_SSFoU-Q_xQ_-p-x5FqzU7J9MZcIaE")

B2_APPLICATION_KEY_ID = os.getenv(
    "B2_APPLICATION_KEY_ID"
)

B2_APPLICATION_KEY = os.getenv(
    "B2_APPLICATION_KEY"
)

B2_BUCKET_NAME = os.getenv(
    "B2_BUCKET_NAME"
)


# Telegram numeric IDs of admins.
#
# Example:
#
# ADMIN_IDS=123456789
#
# Multiple admins:
#
# ADMIN_IDS=123456789,987654321

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv(
        "ADMIN_IDS",
        ""
    ).split(",")
    if x.strip()
}


# Temporary download directory

TEMP_DIR = Path(
    os.getenv(
        "TEMP_DIR",
        "./downloads"
    )
)

TEMP_DIR.mkdir(
    parents=True,
    exist_ok=True
)


# Application-level maximum.
# 2 GiB

MAX_FILE_SIZE = (
    2 * 1024 * 1024 * 1024
)


# Number of folders shown per page

PAGE_SIZE = 20


# ============================================================
# AUTO HLS REMUX + SUPABASE
# ============================================================

SUPABASE_URL = os.getenv(
    "SUPABASE_URL",
    ""
)

SUPABASE_SERVICE_ROLE_KEY = os.getenv(
    "SUPABASE_SERVICE_ROLE_KEY",
    ""
)

# Public base URL where B2 files are reachable.
#
# B2 native:
#   https://f005.backblazeb2.com/file/YOUR_BUCKET
#
# or your Cloudflare CDN domain:
#   https://cdn.yourdomain.com

B2_PUBLIC_BASE_URL = os.getenv(
    "B2_PUBLIC_BASE_URL",
    ""
).rstrip("/")


# Automatically remux video files to HLS
# before uploading (true/false)

AUTO_HLS = (
    os.getenv(
        "AUTO_HLS",
        "true"
    ).lower()
    == "true"
)


VIDEO_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".avi",
    ".webm",
    ".mov",
    ".m4v",
    ".ts",
    ".mpg",
    ".mpeg",
}


supabase = None


if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:

    from supabase import create_client

    supabase = create_client(
        SUPABASE_URL,
        SUPABASE_SERVICE_ROLE_KEY
    )


# Serialize heavy ffmpeg jobs so two
# uploads never transcode at the same time

transcode_lock = asyncio.Lock()


# ============================================================
# BACKBLAZE B2
# ============================================================

info = InMemoryAccountInfo()

b2 = B2Api(info)

bucket = None


# ============================================================
# USER STATE
# ============================================================

# Stores selected folder for each admin

selected_folder = {}


# ============================================================
# ADMIN SECURITY
# ============================================================

def is_admin(update: Update) -> bool:

    user = update.effective_user

    if not user:
        return False

    return user.id in ADMIN_IDS


async def deny(update: Update):

    if update.message:

        await update.message.reply_text(
            "⛔ Admin access only."
        )

    elif update.callback_query:

        await update.callback_query.answer(
            "⛔ Admin access only.",
            show_alert=True
        )


# ============================================================
# DISCOVER B2 FOLDERS
# ============================================================

def discover_folders():

    """
    Backblaze B2 doesn't use real folders.

    Folders are inferred from object-name prefixes.

    Example:

        anime/season-1/episode-01.mp4

    becomes:

        anime/
        anime/season-1/
    """

    folders = set()

    for file_version, _ in bucket.ls(
        folder_to_list="",
        recursive=True,
        fetch_count=1000
    ):

        file_name = file_version.file_name

        parts = (
            file_name
            .strip("/")
            .split("/")
        )

        # Ignore the filename itself.
        # Every preceding part is a folder.

        for i in range(
            1,
            len(parts)
        ):

            folder = "/".join(
                parts[:i]
            )

            folders.add(
                folder + "/"
            )

    return sorted(
        folders,
        key=str.lower
    )


# ============================================================
# FOLDER BUTTONS
# ============================================================

def get_folder_buttons(page=0):

    folders = discover_folders()

    start = page * PAGE_SIZE

    end = start + PAGE_SIZE

    visible_folders = folders[
        start:end
    ]

    buttons = []

    # Folder buttons

    for folder_name in visible_folders:

        buttons.append([
            InlineKeyboardButton(
                f"📁 {folder_name}",
                callback_data=(
                    f"select|{folder_name}"
                )
            )
        ])


    # Navigation

    navigation = []


    if page > 0:

        navigation.append(
            InlineKeyboardButton(
                "⬅️ Previous",
                callback_data=(
                    f"page|{page - 1}"
                )
            )
        )


    if end < len(folders):

        navigation.append(
            InlineKeyboardButton(
                "Next ➡️",
                callback_data=(
                    f"page|{page + 1}"
                )
            )
        )


    if navigation:

        buttons.append(
            navigation
        )


    # Refresh

    buttons.append([
        InlineKeyboardButton(
            "🔄 Refresh folders",
            callback_data="refresh"
        )
    ])


    return (
        folders,
        InlineKeyboardMarkup(buttons)
    )


# ============================================================
# /START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(update):

        await deny(update)

        return


    await update.message.reply_text(

        "👑 *Anime4u B2 Admin Uploader*\n\n"

        "📤 Upload files to Backblaze B2.\n\n"

        "Commands:\n"

        "/folders — Show B2 folders\n"
        "/upload — Choose folder and upload\n"
        "/newfolder — Use a new folder\n"
        "/selected — Show selected folder\n"
        "/cancel — Cancel selection",

        parse_mode="Markdown"
    )


# ============================================================
# /FOLDERS
# ============================================================

async def folders_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(update):

        await deny(update)

        return


    folders, keyboard = (
        get_folder_buttons(0)
    )


    if not folders:

        await update.message.reply_text(

            "📂 No folders found.\n\n"

            "Use /newfolder to create "
            "a new folder path."
        )

        return


    await update.message.reply_text(

        f"📂 *Existing B2 folders*\n\n"
        f"Found: `{len(folders)}`",

        reply_markup=keyboard,

        parse_mode="Markdown"
    )


# ============================================================
# /UPLOAD
# ============================================================

async def upload_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(update):

        await deny(update)

        return


    folders, keyboard = (
        get_folder_buttons(0)
    )


    if not folders:

        await update.message.reply_text(

            "📂 There are no existing folders.\n\n"

            "Use /newfolder to choose a new "
            "B2 folder."
        )

        return


    await update.message.reply_text(

        "📁 *Select an existing B2 folder:*",

        reply_markup=keyboard,

        parse_mode="Markdown"
    )


# ============================================================
# /NEWFOLDER
# ============================================================

async def newfolder_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(update):

        await deny(update)

        return


    context.user_data[
        "waiting_for_folder_name"
    ] = True


    await update.message.reply_text(

        "📁 *New folder path*\n\n"

        "Send the folder path you want.\n\n"

        "Example:\n"
        "`solo-leveling/season-1`\n\n"

        "After selecting the folder, "
        "send your video/file.",

        parse_mode="Markdown"
    )


# ============================================================
# /SELECTED
# ============================================================

async def selected_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(update):

        await deny(update)

        return


    folder = selected_folder.get(
        update.effective_user.id
    )


    if folder:

        await update.message.reply_text(

            "📁 *Currently selected folder:*\n\n"
            f"`{folder}/`",

            parse_mode="Markdown"
        )

    else:

        await update.message.reply_text(
            "📂 No folder selected."
        )


# ============================================================
# /CANCEL
# ============================================================

async def cancel_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(update):

        await deny(update)

        return


    selected_folder.pop(
        update.effective_user.id,
        None
    )


    context.user_data.clear()


    await update.message.reply_text(
        "❌ Operation cancelled."
    )


# ============================================================
# BUTTON HANDLER
# ============================================================

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query


    if not is_admin(update):

        await query.answer(
            "⛔ Admin access only.",
            show_alert=True
        )

        return


    await query.answer()


    data = query.data


    # ========================================================
    # PAGE
    # ========================================================

    if data.startswith("page|"):

        page = int(
            data.split("|", 1)[1]
        )


        _, keyboard = (
            get_folder_buttons(page)
        )


        await query.edit_message_text(

            "📂 *Existing B2 folders:*",

            reply_markup=keyboard,

            parse_mode="Markdown"
        )

        return


    # ========================================================
    # REFRESH
    # ========================================================

    if data == "refresh":

        _, keyboard = (
            get_folder_buttons(0)
        )


        await query.edit_message_text(

            "🔄 *Folders refreshed:*",

            reply_markup=keyboard,

            parse_mode="Markdown"
        )

        return


    # ========================================================
    # SELECT FOLDER
    # ========================================================

    if data.startswith("select|"):

        folder_name = (
            data
            .split("|", 1)[1]
            .strip("/")
        )


        selected_folder[
            query.from_user.id
        ] = folder_name


        await query.edit_message_text(

            "✅ *Folder selected!*\n\n"

            f"📁 `{folder_name}/`\n\n"

            "Now send the video/file.\n\n"

            "Maximum application limit: "
            "`2 GB`.",

            parse_mode="Markdown"
        )


# ============================================================
# NEW FOLDER NAME
# ============================================================

async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(update):

        await deny(update)

        return


    if not context.user_data.get(
        "waiting_for_folder_name"
    ):

        return


    folder_name = (
        update.message.text
        .strip()
        .strip("/")
    )


    # Security checks

    if not folder_name:

        await update.message.reply_text(
            "❌ Folder name cannot be empty."
        )

        return


    if ".." in folder_name:

        await update.message.reply_text(
            "❌ Invalid folder path."
        )

        return


    if "\\" in folder_name:

        await update.message.reply_text(
            "❌ Invalid folder path."
        )

        return


    # Save folder

    selected_folder[
        update.effective_user.id
    ] = folder_name


    context.user_data.pop(
        "waiting_for_folder_name",
        None
    )


    await update.message.reply_text(

        "✅ *Folder selected!*\n\n"

        f"📁 `{folder_name}/`\n\n"

        "Now send the video/file.",

        parse_mode="Markdown"
    )


# ============================================================
# FFMPEG HLS REMUX
# ============================================================

def remux_to_hls(
    local_path: Path,
    out_dir: Path,
    slug: str
) -> Path:

    """
    Copy the video stream (no re-encode),
    re-encode audio to AAC, produce HLS.
    """

    out_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    playlist = (
        out_dir / f"{slug}.m3u8"
    )

    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", str(local_path),
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "96k",
            "-fflags", "+genpts",
            "-hls_time", "6",
            "-hls_playlist_type", "vod",
            "-hls_segment_filename",
            str(out_dir / f"{slug}_%03d.ts"),
            str(playlist),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    return playlist


# ============================================================
# HLS CONTENT TYPES
# ============================================================

B2_CONTENT_TYPES = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".ts": "video/mp2t",
    ".m4s": "video/iso.segment",
    ".mp4": "video/mp4",
}


# ============================================================
# UPLOAD HLS FOLDER
# ============================================================

def upload_hls_folder(
    out_dir: Path,
    b2_folder: str
) -> str:

    """
    Upload every file produced by ffmpeg
    into b2_folder on B2.

    Returns the public URL of the playlist.
    """

    playlist_url = None


    for f in sorted(
        out_dir.iterdir()
    ):

        if not f.is_file():
            continue


        key = f"{b2_folder}/{f.name}"


        bucket.upload_local_file(

            local_file=str(f),

            file_name=key,

            content_type=(
                B2_CONTENT_TYPES.get(
                    f.suffix.lower()
                )
            ),
        )


        if f.suffix == ".m3u8":

            playlist_url = (

                f"{B2_PUBLIC_BASE_URL}/"
                f"{key}"
            )


    return playlist_url


# ============================================================
# SUPABASE ROW INSERT
# ============================================================

def insert_stream_row(
    title: str,
    video_url: str
) -> None:

    supabase.table(
        "streams"
    ).insert({

        "title": title,

        "video_url": video_url,

        "is_live": True,

    }).execute()


# ============================================================
# FILE UPLOAD
# ============================================================

async def handle_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(update):

        await deny(update)

        return


    user_id = update.effective_user.id


    # ========================================================
    # CHECK FOLDER
    # ========================================================

    folder_name = selected_folder.get(
        user_id
    )


    if not folder_name:

        await update.message.reply_text(

            "📂 No folder selected.\n\n"

            "Use /upload first."
        )

        return


    # ========================================================
    # GET MEDIA
    # ========================================================

    document = update.message.document

    video = update.message.video


    media = (
        document
        or video
    )


    if not media:

        return


    # ========================================================
    # CHECK FILE SIZE
    # ========================================================

    file_size = getattr(
        media,
        "file_size",
        None
    )


    if (
        file_size
        and file_size > MAX_FILE_SIZE
    ):

        await update.message.reply_text(

            "❌ File is larger than 2 GB."
        )

        return


    # ========================================================
    # FILE NAME
    # ========================================================

    file_name = getattr(
        media,
        "file_name",
        None
    )


    if not file_name:

        file_name = (
            f"{media.file_unique_id}.bin"
        )


    # Prevent path traversal

    safe_name = Path(
        file_name
    ).name


    if not safe_name:

        await update.message.reply_text(
            "❌ Invalid filename."
        )

        return


    # ========================================================
    # TEMPORARY FILE
    # ========================================================

    local_path = (
        TEMP_DIR / safe_name
    )


    status = await update.message.reply_text(

        "⬇️ *Downloading from Telegram...*\n\n"

        "Large files can take some time.",

        parse_mode="Markdown"
    )


    # ========================================================
    # DOWNLOAD
    # ========================================================

    try:

        telegram_file = (
            await context.bot.get_file(
                media.file_id
            )
        )


        await telegram_file.download_to_drive(
            custom_path=str(local_path)
        )


    except Exception as error:

        await status.edit_text(

            "❌ Telegram download failed.\n\n"
            f"`{str(error)[:800]}`",

            parse_mode="Markdown"
        )


        try:

            local_path.unlink()

        except OSError:

            pass


        return


    # ========================================================
    # ACTUAL SIZE CHECK
    # ========================================================

    actual_size = (
        local_path.stat().st_size
    )


    if actual_size > MAX_FILE_SIZE:

        try:

            local_path.unlink()

        except OSError:

            pass


        await status.edit_text(

            "❌ File exceeds the 2 GB limit."
        )

        return


    # ========================================================
    # AUTO HLS REMUX + SUPABASE
    # ========================================================

    file_suffix = (
        Path(safe_name)
        .suffix
        .lower()
    )


    if (
        AUTO_HLS
        and B2_PUBLIC_BASE_URL
        and file_suffix in VIDEO_EXTENSIONS
    ):

        async with transcode_lock:

            slug = (
                Path(safe_name)
                .stem
            )

            slug = "".join(

                c
                if (
                    c.isalnum()
                    or c in "-_"
                )
                else "_"

                for c in slug
            )


            hls_dir = (

                TEMP_DIR
                / f"{slug}_hls"
            )


            # ------------------------------------------------
            # REMUX
            # ------------------------------------------------

            try:

                await status.edit_text(

                    "🔄 *Remuxing to HLS...*\n\n"
                    "Copying video stream, "
                    "this takes a moment.",

                    parse_mode="Markdown"
                )


                await asyncio.to_thread(

                    remux_to_hls,

                    local_path,

                    hls_dir,

                    slug
                )


            except Exception as error:

                # ffmpeg failed — fall back to
                # uploading the original file

                await status.edit_text(

                    "⚠️ Remux failed — uploading "
                    "the original file instead.\n\n"

                    f"`{str(error)[:500]}`",

                    parse_mode="Markdown"
                )


                shutil.rmtree(

                    hls_dir,

                    ignore_errors=True
                )


            else:

                # ------------------------------------------------
                # UPLOAD HLS FILES
                # ------------------------------------------------

                b2_folder = (

                    f"{folder_name}/{slug}"
                )


                await status.edit_text(

                    "☁️ *Uploading HLS files "
                    "to Backblaze B2...*",

                    parse_mode="Markdown"
                )


                try:

                    playlist_url = (

                        await asyncio.to_thread(

                            upload_hls_folder,

                            hls_dir,

                            b2_folder
                        )
                    )


                except Exception as error:

                    await status.edit_text(

                        "❌ HLS upload failed.\n\n"
                        f"`{str(error)[:800]}`",

                        parse_mode="Markdown"
                    )


                    shutil.rmtree(

                        hls_dir,

                        ignore_errors=True
                    )


                    try:

                        local_path.unlink()

                    except OSError:

                        pass


                    return


                # ------------------------------------------------
                # SUPABASE ROW
                # ------------------------------------------------

                supabase_ok = False


                if supabase and playlist_url:

                    try:

                        await asyncio.to_thread(

                            insert_stream_row,

                            Path(safe_name).stem,

                            playlist_url
                        )


                        supabase_ok = True


                    except Exception as error:

                        print(
                            "Supabase insert failed: "
                            f"{error}"
                        )


                # ------------------------------------------------
                # CLEANUP
                # ------------------------------------------------

                shutil.rmtree(

                    hls_dir,

                    ignore_errors=True
                )


                try:

                    local_path.unlink()

                except OSError:

                    pass


                # ------------------------------------------------
                # SUCCESS MESSAGE
                # ------------------------------------------------

                message_text = (

                    "✅ *UPLOAD COMPLETE*\n\n"

                    f"📄 *File:*\n"
                    f"`{safe_name}`\n\n"

                    f"📁 *Folder:*\n"
                    f"`{b2_folder}/`\n\n"

                    f"🔗 *Playlist:*\n"
                    f"`{playlist_url}`"
                )


                if supabase_ok:

                    message_text += (

                        "\n\n✨ Added to your "
                        "streaming site."
                    )


                else:

                    message_text += (

                        "\n\n⚠️ Not added to "
                        "Supabase — insert the row "
                        "manually."
                    )


                await status.edit_text(

                    message_text,

                    parse_mode="Markdown"
                )


                return


    # ========================================================
    # B2 OBJECT PATH
    # ========================================================

    b2_path = (
        f"{folder_name}/{safe_name}"
    )


    await status.edit_text(

        "☁️ *Uploading to Backblaze B2...*\n\n"

        f"📁 `{folder_name}/`\n"
        f"📄 `{safe_name}`",

        parse_mode="Markdown"
    )


    # ========================================================
    # UPLOAD TO B2
    # ========================================================

    try:

        result = await asyncio.to_thread(

            bucket.upload_local_file,

            local_file=str(
                local_path
            ),

            file_name=b2_path
        )


    except Exception as error:

        await status.edit_text(

            "❌ B2 upload failed.\n\n"
            f"`{str(error)[:1000]}`",

            parse_mode="Markdown"
        )


        try:

            local_path.unlink()

        except OSError:

            pass


        return


    # ========================================================
    # DELETE TEMP FILE
    # ========================================================

    try:

        local_path.unlink()

    except OSError:

        pass


    # ========================================================
    # B2 FILE ID
    # ========================================================

    b2_file_id = getattr(
        result,
        "id_",
        "unknown"
    )


    # ========================================================
    # SUCCESS
    # ========================================================

    await status.edit_text(

        "✅ *UPLOAD COMPLETE*\n\n"

        f"📄 *File:*\n"
        f"`{safe_name}`\n\n"

        f"📁 *Folder:*\n"
        f"`{folder_name}/`\n\n"

        f"📦 *B2 Path:*\n"
        f"`{b2_path}`\n\n"

        f"🆔 *B2 File ID:*\n"
        f"`{b2_file_id}`\n\n"

        "The file is now stored in B2.\n\n"

        "You can use its B2 path/URL "
        "when creating your schedule "
        "in Supabase.",

        parse_mode="Markdown"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    global bucket


    # ========================================================
    # CONFIG CHECK
    # ========================================================

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN is missing."
        )


    if not B2_APPLICATION_KEY_ID:

        raise RuntimeError(
            "B2_APPLICATION_KEY_ID is missing."
        )


    if not B2_APPLICATION_KEY:

        raise RuntimeError(
            "B2_APPLICATION_KEY is missing."
        )


    if not B2_BUCKET_NAME:

        raise RuntimeError(
            "B2_BUCKET_NAME is missing."
        )


    if not ADMIN_IDS:

        raise RuntimeError(

            "ADMIN_IDS is required.\n\n"

            "Example:\n"
            "ADMIN_IDS=123456789"
        )


    # ========================================================
    # CONNECT B2
    # ========================================================

    print(
        "☁️ Connecting to Backblaze B2..."
    )


    b2.authorize_account(

        "production",

        B2_APPLICATION_KEY_ID,

        B2_APPLICATION_KEY
    )


    bucket = (
        b2.get_bucket_by_name(
            B2_BUCKET_NAME
        )
    )


    print(
        f"✅ Connected to bucket: "
        f"{B2_BUCKET_NAME}"
    )


    # ========================================================
    # TELEGRAM APPLICATION
    # ========================================================

    application = (
        Application
       .builder()
        .token(BOT_TOKEN)
        .build()
    )



    # Commands

    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )


    application.add_handler(
        CommandHandler(
            "folders",
            folders_command
        )
    )


    application.add_handler(
        CommandHandler(
            "upload",
            upload_command
        )
    )


    application.add_handler(
        CommandHandler(
            "newfolder",
            newfolder_command
        )
    )


    application.add_handler(
        CommandHandler(
            "selected",
            selected_command
        )
    )


    application.add_handler(
        CommandHandler(
            "cancel",
            cancel_command
        )
    )


    # Inline buttons

    application.add_handler(
        CallbackQueryHandler(
            callback_handler
        )
    )


    # Videos and documents

    application.add_handler(
        MessageHandler(
            filters.Document.ALL
            | filters.VIDEO,
            handle_file
        )
    )


    # Folder name input

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_text
        )
    )


    print(
        "🚀 Anime4u B2 Admin Uploader is running."
    )


    application.run_polling()


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    main()
