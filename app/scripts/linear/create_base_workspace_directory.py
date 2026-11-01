#!/usr/bin/env python3
"""Create base workspace directory structure in Dropbox.

Usage:
    python -m app.scripts.linear.create_base_workspace_directory
"""

import os
import re

import dropbox
from dotenv import load_dotenv
from services.dropbox.client import get_dropbox_client as _get_dropbox_client

load_dotenv()


# Workspace name (configurable via env var)
WORKSPACE_NAME = os.getenv('OBSIDIAN_LINEAR_WORKSPACE_NAME', '_Chapters-Technology')

# Directory structure to create under {prefix}_Workspaces/{WORKSPACE_NAME}/
WORKSPACE_STRUCTURE = [
    '_Planning',
    '_Initiatives',
    '_Initiatives/00_Active',
    '_Initiatives/01_Planned',
    '_Initiatives/02_Completed',
    '_Initiatives/03_Archived',
]



def _get_root_folders(dbx: dropbox.Dropbox, vault_path: str) -> list[str]:
    """Get all root-level folder names from the vault."""
    folders = []
    result = dbx.files_list_folder(vault_path)

    while True:
        for entry in result.entries:
            if isinstance(entry, dropbox.files.FolderMetadata):
                folders.append(entry.name)

        if not result.has_more:
            break
        result = dbx.files_list_folder_continue(result.cursor)

    return folders


def _find_highest_prefix(folders: list[str]) -> int:
    """Find the highest numbered prefix from folder names.

    Expects folders with names like '00_Something', '01_Other', etc.
    Returns the highest number found, or -1 if no prefixed folders exist.
    """
    pattern = re.compile(r'^(\d+)_')
    highest = -1

    for folder in folders:
        match = pattern.match(folder)
        if match:
            num = int(match.group(1))
            if num > highest:
                highest = num

    return highest


def _create_folder_if_not_exists(dbx: dropbox.Dropbox, path: str) -> bool:
    """Create a folder in Dropbox if it doesn't exist.

    Returns True if folder was created, False if it already existed.
    """
    try:
        dbx.files_create_folder_v2(path)
        print(f"  Created: {path}")
        return True
    except dropbox.exceptions.ApiError as e:
        if isinstance(e.error, dropbox.files.CreateFolderError):
            if e.error.is_path() and e.error.get_path().is_conflict():
                print(f"  Exists:  {path}")
                return False
        raise


def create_workspace_structure() -> dict:
    """Create the base workspace directory structure in Dropbox.

    Returns a dict with success status and details.
    """
    vault_path = os.getenv('DROPBOX_OBSIDIAN_VAULT_PATH')
    if not vault_path:
        return {'success': False, 'error': 'DROPBOX_OBSIDIAN_VAULT_PATH not set'}

    try:
        dbx = _get_dropbox_client()

        # Get all root folders and find highest prefix
        print("Scanning root folders...")
        root_folders = _get_root_folders(dbx, vault_path)
        highest_prefix = _find_highest_prefix(root_folders)
        next_prefix = highest_prefix + 1

        print(f"Found highest prefix: {highest_prefix:02d}")
        print(f"Using next prefix: {next_prefix:02d}")

        # Build the base path
        workspace_folder = f"{next_prefix:02d}_Workspaces"
        base_path = f"{vault_path}/{workspace_folder}/{WORKSPACE_NAME}"

        print(f"\nCreating workspace structure at: {base_path}")

        # Create the base workspace folder first
        _create_folder_if_not_exists(dbx, f"{vault_path}/{workspace_folder}")
        _create_folder_if_not_exists(dbx, base_path)

        # Create all subdirectories
        created_count = 0
        for subdir in WORKSPACE_STRUCTURE:
            full_path = f"{base_path}/{subdir}"
            if _create_folder_if_not_exists(dbx, full_path):
                created_count += 1

        print(f"\nDone! Created {created_count} new folders.")

        return {
            'success': True,
            'workspace_path': base_path,
            'prefix': f"{next_prefix:02d}",
            'created_count': created_count
        }

    except EnvironmentError as e:
        return {'success': False, 'error': str(e)}
    except dropbox.exceptions.ApiError as e:
        return {'success': False, 'error': f"Dropbox API error: {e}"}
    except Exception as e:
        return {'success': False, 'error': f"Unexpected error: {e}"}


def main():
    result = create_workspace_structure()
    if result['success']:
        print(f"\nWorkspace created at: {result['workspace_path']}")
    else:
        print(f"\nError: {result['error']}")


if __name__ == "__main__":
    main()
