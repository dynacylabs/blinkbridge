"""Utility functions for process and file system operations.

Provides functions to interact with Linux /proc filesystem for monitoring
process file descriptors and waiting for specific file operations.
"""
import time
from pathlib import Path
from typing import List, Union


def get_pids_by_name(process_name: str) -> List[int]:
    """Get all process IDs with the given process name.
    
    Searches /proc filesystem for processes matching the specified name.
    
    Args:
        process_name: Name of the process to search for (from /proc/[pid]/comm)
        
    Returns:
        List of process IDs matching the name. Empty list if none found.
        
    Example:
        >>> pids = get_pids_by_name('ffmpeg')
        >>> print(pids)
        [1234, 5678]
    """
    pids = []

    for pid_dir in Path('/proc').iterdir():
        if not (pid_dir.is_dir() and pid_dir.name.isdigit()):
            continue
            
        try:
            with open(pid_dir / 'comm', 'r') as f:
                comm = f.read().strip()
                if comm == process_name:
                    pids.append(int(pid_dir.name))
        except FileNotFoundError:
            # Process may have terminated between directory listing and file read
            continue

    return pids

def get_open_files(pid: int) -> List[Path]:
    """Get all files currently opened by a process.
    
    Reads the /proc/[pid]/fd directory to find all open file descriptors.
    
    Args:
        pid: Process ID to check
        
    Returns:
        List of Path objects for open files. Empty list if process not found
        or no files are open.
        
    Note:
        Symbolic links in /proc/[pid]/fd point to the actual file paths.
        This function resolves those links to get the real paths.
    """
    file_names = []
    fd_dir = Path(f'/proc/{pid}/fd')

    if not fd_dir.is_dir():
        return file_names
    
    for fd in fd_dir.iterdir():
        try:
            file_names.append(fd.resolve())
        except (FileNotFoundError, PermissionError):
            # FD may have been closed or we lack permission to read it
            continue
        
    return file_names
 
def is_file_open(process_name: str, file_name: Union[str, Path]) -> bool:
    """Check if a file is currently open by any process with the given name.
    
    Useful for verifying that a media file is actively being read/written.
    
    Args:
        process_name: Name of the process(es) to check
        file_name: Path to the file to check
        
    Returns:
        True if the file is open by any matching process, False otherwise.
        
    Example:
        >>> is_file_open('ffmpeg', '/tmp/video.mp4')
        True
    """
    file_name = Path(file_name).resolve()
    pids = get_pids_by_name(process_name)

    for pid in pids:
        open_files = get_open_files(pid)
        if file_name in open_files:
            return True
                
    return False

def wait_until_file_open(file_path: Union[str, Path], pid: int, timeout: float=10.0, poll_interval: float=0.1) -> float:
    """Wait until a file is opened by a specific process.
    
    Polls the process's open file descriptors until the specified file appears.
    Used to synchronize video streaming operations.
    
    Args:
        file_path: Path to the file to monitor
        pid: Process ID to check
        timeout: Maximum time to wait in seconds (default: 10.0)
        poll_interval: How often to check in seconds (default: 0.1)
        
    Returns:
        Time elapsed in seconds until file was opened.
        
    Raises:
        TimeoutError: If timeout is reached before file is opened by the process.
        
    Example:
        >>> elapsed = wait_until_file_open('/tmp/video.mp4', 1234, timeout=5.0)
        >>> print(f"File opened after {elapsed:.2f} seconds")
        File opened after 0.47 seconds
    """
    file_path = Path(file_path).resolve()
    start_time = time.time()

    while time.time() - start_time <= timeout:
        open_files = get_open_files(pid)
        
        if file_path in open_files:
            return time.time() - start_time

        time.sleep(poll_interval)

    raise TimeoutError(f"Timeout waiting for process {pid} to open {file_path}")