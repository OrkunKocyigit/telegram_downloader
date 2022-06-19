import argparse
import asyncio
import json
import os.path
import pathlib

from telethon.sync import TelegramClient
from telethon.tl.custom import Message


async def write_state_to_file(state: dict, user_id: str) -> None:
    # If user_id is not in state add it
    if user_id not in state:
        state[user_id] = {}
    # Get state for this user_id
    user_state = state[user_id]
    # Remove message instance from value as it can't be written into a file
    for state_item in user_state.values():
        if "message" in state_item:
            del state_item["message"]
    # Write into a file
    with open("state.json", "w", encoding="utf8") as state_file:
        json.dump(state, state_file, indent=2, ensure_ascii=False)


async def read_state_from_file() -> dict:
    # Check if state.json exists
    if os.path.isfile('./state.json'):
        # If it exists then return it
        with open("state.json", "r", encoding="utf8") as state_file:
            return json.load(state_file)
    # If state doesn't exist return an empty state aka empty dict
    return {}


async def get_last_id(state: dict, user_id: str) -> int:
    # Check if we have this chat in state before
    if user_id in state:
        # If we do iterate in state
        last_key = 0
        for key, value in state[user_id].items():
            last_key = key
            # Return message id of first item we have not downloaded
            if value["has_media"] and not value["downloaded"]:
                return int(last_key)
        # We can't find any messages we have not downloaded so return id of
        # last one
        return int(last_key)
    # State doesn't have this chat, so we add it then return 0 aka first message
    state[user_id] = {}
    return 0


async def get_file_path(message: Message) -> str:
    # Path has two requirements, first we need file name and extension
    # 2nd there should be no already existing file with that name
    if message.file.name is not None:
        # If message already has a file name use it
        file_name: str = message.file.name
        # Replace file name extension
        file_name = file_name[:-len(message.file.ext)]
    else:
        # If not we use message id plus date
        file_name = "{} - {}".format(message.id,
                                     message.date.strftime("%Y%m%d-%H%M"))
    # Get media type for the folder
    mime = message.file.mime_type
    if "image" in mime:
        media_type = "images"
    elif "video" in mime:
        media_type = "videos"
    else:
        media_type = "other"
    # Create path for a result
    path = pathlib.Path("media", media_type,
                        "{}{}".format(file_name, message.file.ext))
    # Check for collisions
    conflict = path.is_file()
    if conflict:
        # If there is conflict we will enumerate file name until there is none
        number = 0
        while conflict:
            # Increment number at the end of file name
            number = number + 1
            # Create a new path
            path = pathlib.Path("media",
                                media_type,
                                "{} - {}{}".format(file_name,
                                                   number,
                                                   message.file.ext))
            # Update if it exists or not
            conflict = path.is_file()
    return str(path)


async def download_messages(client: TelegramClient,
                            download_queue: list[dict],
                            chat_state: dict) -> None:
    # Get message instances from queue items as they are only requirement
    messages: list[Message] = [x["message"] for x in download_queue]
    # Create async task for each file
    download_tasks = [
        client.download_media(message, await get_file_path(message))
        for message in messages
    ]
    # Run all tasks
    file_paths = await asyncio.gather(*download_tasks)
    # Enumerate in messages to update successful download
    for count, message in enumerate(messages):
        # Library retuns a valid path in case download is successful
        # So first step we'll check if result is not None
        if file_paths[count] is not None:
            # 2nd step we'll get path and check if it is a file
            file_path = file_paths[count]
            if pathlib.Path(file_path).is_file():
                # If so we cna update the state
                message_id = str(message.id)
                chat_state[message_id]["downloaded"] = True


async def run(client: TelegramClient, user_id: str) -> None:
    # Get state
    state = await read_state_from_file()
    try:
        # Get chat for processing
        chat = await client.get_entity(user_id)
        # Get last not downloaded message to skip already processed messages
        last_id = await get_last_id(state, user_id)
        messages = client.iter_messages(entity=chat,
                                        reverse=True,
                                        min_id=last_id)
        # Iterate in messages and add in a queue, basic idea is we will
        # download 10 media at a time, queue will be filled with main thread
        # then, download tasks will split into smaller threads
        chat_state = state[user_id]
        download_queue = []
        async for message in messages:
            # Get message id and convert it into str for dict key
            message_id = str(message.id)
            # Check if message is in state already
            if message_id not in chat_state:
                # If message is not in state we should add to state
                if message.media is not None and message.file is not None:
                    # If message is not in state and has media it means
                    # we couldn't have downloaded it yet, henceforth
                    # we should add to state then add to download queue
                    state_item = {"has_media": True, "downloaded": False,
                                  "message": message}
                    chat_state[message_id] = state_item
                    download_queue.append(state_item)
                else:
                    # We don't care if it doesn't have any media, so we add it
                    # to state to skip it next time
                    chat_state[message_id] = {"has_media": False}
            else:
                # If it is in state we should check if it has media, and we
                # didn't download it before
                state_item = chat_state[message_id]
                if state_item["has_media"] and not state_item["downloaded"]:
                    state_item["message"] = message
                    download_queue.append(state_item)
            # If we have more than 10 items right now start downloading
            if len(download_queue) >= 20:
                await download_messages(client, download_queue, chat_state)
                # Once download finish, update the state file
                await write_state_to_file(state, user_id)
                # And clear the download queue so we don't download
                download_queue.clear()
        # Download any item leftover because we couldn't hit 10 message limit
        if len(download_queue) >= 0:
            await download_messages(client, download_queue, chat_state)
    finally:
        await write_state_to_file(state, user_id)


def create_argparser() -> argparse.ArgumentParser:
    # We need 3 variables in our application. App_id, api_hash and user_id
    # Create generic parser
    parser = argparse.ArgumentParser(
        "Telegram Downloader",
        description="Download all media from Telegram",
        add_help=True
    )
    # Add parameters
    parser.add_argument("-a", "--app-id", type=int, required=True)
    parser.add_argument("-k", "--api-hash", type=str, required=True)
    parser.add_argument("-u", "--user-id", type=str, required=True)
    return parser


def main() -> None:
    # Set Parameters
    arg_parser = create_argparser()
    arguments = vars(arg_parser.parse_args())
    app_id = arguments["app-id"]
    api_hash = arguments["api-hash"]
    user_id = arguments["user-id"]
    # Create telegram client
    with TelegramClient('name', app_id, api_hash) as client:
        client.loop.run_until_complete(run(client, user_id))


if __name__ == "__main__":
    # Run the sync application
    main()
