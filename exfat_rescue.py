import os
import sys
import struct
import time
import re
import json
import threading
import queue
import webbrowser
import bisect
import ctypes
import ctypes.wintypes
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# ==============================================================================
# ExFatRescue v1.3.0 - Production-Grade exFAT Raw File & Project Recovery Tool
# Special support for Actual Discovered Folders, Godot/Unity game projects,
# 3D assets, source code, and Interactive Visual Disk Map
# Author: Mikel Bryan (Kl3tus)
# ==============================================================================

FALLBACK_DISK_BYTES = 511868665856  # Exact size of 512GB SDXC (Generic MassStorageClass)
CLUSTER_SIZE = 128 * 1024          # 128 KB standard cluster
CLUSTER_HEAP_OFFSET = 32 * 1024 * 1024 # 32 MB cluster heap start

CATEGORY_MAP = {
    'Godot / Game': ['.godot', '.gd', '.tscn', '.tres', '.import', '.gdextension', '.shader', '.xnb', '.assets', '.ress', '.res', '.sav', '.xwb', '.mgfxo', '.bundle', '.pak'],
    '3D & Assets': ['.blend', '.blend1', '.fbx', '.obj', '.gltf', '.glb', '.dae', '.psd'],
    'Code & Dev': ['.java', '.py', '.cpp', '.c', '.h', '.hpp', '.cs', '.kt', '.rs', '.js', '.ts', '.html', '.css', '.json', '.xml', '.yaml', '.yml', '.gradle', '.dll', '.so', '.dylib', '.exe', '.config', '.ini', '.map'],
    'Archives': ['.zip', '.7z', '.rar', '.tar', '.gz', '.bz2', '.xz', '.iso', '.vdf'],
    'Media': ['.png', '.jpg', '.jpeg', '.webp', '.svg', '.gif', '.bmp', '.mp4', '.mkv', '.avi', '.mov', '.mp3', '.wav', '.ogg', '.flac'],
    'Documents': ['.pdf', '.txt', '.md', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.csv', '.rtf']
}

CATEGORY_COLORS = {
    '📁 Folder':    {'fill': '#1e293b', 'border': '#38bdf8', 'accent': '#7dd3fc', 'subfill': '#0f172a'},
    'Godot / Game': {'fill': '#064e3b', 'border': '#10b981', 'accent': '#34d399', 'subfill': '#04382a'},
    '3D & Assets':  {'fill': '#4c1d95', 'border': '#8b5cf6', 'accent': '#a78bfa', 'subfill': '#3b1675'},
    'Code & Dev':   {'fill': '#1e3a8a', 'border': '#3b82f6', 'accent': '#60a5fa', 'subfill': '#172e6d'},
    'Archives':     {'fill': '#78350f', 'border': '#f59e0b', 'accent': '#fbbf24', 'subfill': '#5c290c'},
    'Media':        {'fill': '#881337', 'border': '#f43f5e', 'accent': '#fb7185', 'subfill': '#660e29'},
    'Documents':    {'fill': '#334155', 'border': '#94a3b8', 'accent': '#cbd5e1', 'subfill': '#1e293b'},
    'Other':        {'fill': '#1f2937', 'border': '#4b5563', 'accent': '#9ca3af', 'subfill': '#111827'}
}

def get_resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath(os.path.dirname(__file__)), relative_path)

def get_physical_drive_size(drive_path):
    """ Queries the exact physical size of the drive in bytes via Windows IOCTL """
    IOCTL_DISK_GET_LENGTH_INFO = 0x00074059
    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 1
    FILE_SHARE_WRITE = 2
    OPEN_EXISTING = 3

    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateFileW(
            drive_path,
            GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            0,
            None
        )
        if handle != -1 and handle != 0xFFFFFFFFFFFFFFFF:
            class GET_LENGTH_INFORMATION(ctypes.Structure):
                _fields_ = [('Length', ctypes.c_int64)]
            length_info = GET_LENGTH_INFORMATION()
            bytes_returned = ctypes.wintypes.DWORD()
            res = kernel32.DeviceIoControl(
                handle,
                IOCTL_DISK_GET_LENGTH_INFO,
                None, 0,
                ctypes.byref(length_info), ctypes.sizeof(length_info),
                ctypes.byref(bytes_returned),
                None
            )
            kernel32.CloseHandle(handle)
            if res and length_info.Length > 0:
                return length_info.Length
    except Exception:
        pass
            
    # Secondary fallback via file seek
    try:
        with open(drive_path, "rb") as f:
            f.seek(0, 2)
            sz = f.tell()
            if sz > 0:
                return sz
    except Exception:
        pass
        
    return FALLBACK_DISK_BYTES

def format_size(bytes_val):
    if bytes_val < 1024:
        return f"{bytes_val} B"
    elif bytes_val < 1024 * 1024:
        return f"{bytes_val / 1024:.1f} KB"
    elif bytes_val < 1024 * 1024 * 1024:
        return f"{bytes_val / (1024 * 1024):.1f} MB"
    else:
        return f"{bytes_val / (1024 * 1024 * 1024):.2f} GB"

def get_file_category(filename, is_dir=False):
    if is_dir:
        return '📁 Folder'
    ext = os.path.splitext(filename)[1].lower()
    for cat, exts in CATEGORY_MAP.items():
        if ext in exts:
            return cat
    return 'Other'

def sanitize_filename(name):
    clean = re.sub(r'[\x00\\/*?:"<>|]', '_', name).strip()
    return clean if clean else "unnamed_file"

def sanitize_rel_path(path_str):
    parts = [sanitize_filename(p) for p in re.split(r'[\\/]', path_str) if p.strip()]
    return os.path.join(*parts) if parts else "unnamed_file"

def parse_exfat_entry_set(buf, offset, max_disk_size):
    """
    Parses an exFAT directory entry set (0x85 File + 0xC0 Stream + 0xC1 Name)
    with strict validation against drive bounds.
    """
    if offset + 32 > len(buf):
        return None
    if buf[offset] != 0x85:
        return None
    
    sec_count = buf[offset + 1]
    if sec_count < 2 or sec_count > 18:
        return None
    
    total_len = 32 * (sec_count + 1)
    if offset + total_len > len(buf):
        return None
    
    attrs = struct.unpack('<H', buf[offset+4:offset+6])[0]
    is_dir = bool(attrs & 0x10)
    
    # 0xC0 Stream Extension entry
    stream_off = offset + 32
    if buf[stream_off] != 0xC0:
        return None
    
    flags = buf[stream_off + 1]
    name_len = buf[stream_off + 3]
    first_cluster = struct.unpack('<I', buf[stream_off+20:stream_off+24])[0]
    data_len = struct.unpack('<Q', buf[stream_off+24:stream_off+32])[0]
    
    max_clusters = max_disk_size // CLUSTER_SIZE
    if first_cluster < 2 or first_cluster >= max_clusters:
        return None
    if not is_dir and (data_len <= 0 or data_len > max_disk_size):
        return None

    # 0xC1 File Name entries
    name_chars = []
    for i in range(2, sec_count + 1):
        fn_off = offset + 32 * i
        if buf[fn_off] != 0xC1:
            break
        raw_part = buf[fn_off+2:fn_off+32].decode('utf-16le', errors='ignore')
        name_chars.append(raw_part)
    
    full_name = "".join(name_chars)[:name_len].split('\x00')[0]
    if not full_name or len(full_name) != name_len:
        return None
    
    if not all(31 < ord(c) < 65534 for c in full_name):
        return None
        
    return {
        'name': full_name,
        'is_dir': is_dir,
        'first_cluster': first_cluster,
        'data_len': data_len,
        'no_fat_chain': bool(flags & 0x02)
    }

# ==============================================================================
# Squarified Treemap Algorithm (Visual Map Layout)
# ==============================================================================

def squarify(children, rect):
    """
    Computes a squarified treemap layout.
    children: list of dicts containing 'size' (float/int)
    rect: (x, y, w, h)
    Returns: list of dicts with calculated 'rect': (rx, ry, rw, rh)
    """
    if not children or rect[2] <= 0 or rect[3] <= 0:
        return []

    total_size = sum(c['size'] for c in children)
    if total_size <= 0:
        return []

    items = sorted(children, key=lambda c: c['size'], reverse=True)
    results = []

    curr_x, curr_y, curr_w, curr_h = rect
    current_rem_size = total_size
    current_row = []

    for item in items:
        if item['size'] <= 0:
            continue
        is_horiz = curr_w >= curr_h
        side = curr_w if is_horiz else curr_h
        other_side = curr_h if is_horiz else curr_w

        def worst_aspect(row, candidate):
            test_row = row + [candidate]
            r_size = sum(x['size'] for x in test_row)
            if r_size <= 0 or current_rem_size <= 0:
                return float('inf')
            row_thick = (r_size / current_rem_size) * other_side
            if row_thick <= 0:
                return float('inf')
            max_aspect = 0
            for x in test_row:
                length = (x['size'] / r_size) * side
                if length <= 0:
                    continue
                asp = max(length / row_thick, row_thick / length)
                if asp > max_aspect:
                    max_aspect = asp
            return max_aspect

        if not current_row:
            current_row.append(item)
        else:
            aspect_with = worst_aspect(current_row, item)
            aspect_without = worst_aspect(current_row[:-1], current_row[-1]) if len(current_row) > 1 else worst_aspect([], current_row[0])
            if aspect_with <= aspect_without:
                current_row.append(item)
            else:
                # Flush row
                r_size = sum(x['size'] for x in current_row)
                if is_horiz:
                    row_h = (r_size / current_rem_size) * curr_h
                    cx = curr_x
                    for it in current_row:
                        w = (it['size'] / r_size) * curr_w
                        results.append({**it, 'rect': (cx, curr_y, max(w, 1), max(row_h, 1))})
                        cx += w
                    curr_y += row_h
                    curr_h -= row_h
                else:
                    row_w = (r_size / current_rem_size) * curr_w
                    cy = curr_y
                    for it in current_row:
                        h = (it['size'] / r_size) * curr_h
                        results.append({**it, 'rect': (curr_x, cy, max(row_w, 1), max(h, 1))})
                        cy += h
                    curr_x += row_w
                    curr_w -= row_w
                current_rem_size -= r_size
                current_row = [item]

    if current_row:
        r_size = sum(x['size'] for x in current_row)
        is_horiz = curr_w >= curr_h
        if is_horiz:
            cx = curr_x
            for it in current_row:
                w = (it['size'] / r_size) * curr_w
                results.append({**it, 'rect': (cx, curr_y, max(w, 1), max(curr_h, 1))})
                cx += w
        else:
            cy = curr_y
            for it in current_row:
                h = (it['size'] / r_size) * curr_h
                results.append({**it, 'rect': (curr_x, cy, max(row_w, 1), max(h, 1))})
                cy += h

    return results

# ==============================================================================
# Modern High-Performance GUI
# ==============================================================================

class ExFatRescueApp:
    def __init__(self, root):
        self.root = root
        self.root.title("ExFatRescue v1.3.0 - exFAT Lost File & Project Recovery")
        self.root.geometry("1180x780")
        self.root.minsize(980, 660)
        
        # Color palette
        self.c_bg = "#14141e"
        self.c_card = "#1e1e2c"
        self.c_card_border = "#2b2b3d"
        self.c_accent = "#2563eb"
        self.c_accent_hover = "#3b82f6"
        self.c_success = "#10b981"
        self.c_danger = "#ef4444"
        self.c_donate = "#e11d48"
        self.c_text = "#f3f4f6"
        self.c_text_dim = "#9ca3af"
        
        self.root.configure(bg=self.c_bg)
        
        # State variables
        self.discovered_items = []  # Discovered item dictionaries
        self.selected_item_indices = set()
        self.selected_count = 0
        self.selected_bytes = 0
        
        self.is_scanning = False
        self.is_recovering = False
        self.stop_requested = False
        self.resume_offset = None
        self.msg_queue = queue.Queue()
        
        self.target_drive_var = tk.StringVar(value=r"\\.\PhysicalDrive1")
        self.output_dir_var = tk.StringVar(value=r"H:\SD Card\ExFatRescue_Output")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", self.on_search_change)
        
        # Filter controls
        self.category_var = tk.StringVar(value="All")
        self.item_type_filter_var = tk.StringVar(value="all") # "all", "folders_only", "files_only"
        self.map_group_var = tk.StringVar(value="Actual Folders") # "Actual Folders", "Category", "Extension (.ext)"
        self.folder_tree_mode_var = tk.StringVar(value="folders") # "folders", "categories"
        
        self.active_view = "list"  # "list", "map", "tree"
        self.search_debounce_timer = None
        self.hovered_tile = None
        self.active_tile_data = None
        self.rendered_tiles = []
        
        # Folder cache for instant lookup
        self.cached_dir_names = set()
        self.sorted_dir_items = []
        
        self.setup_styles()
        self.build_ui()
        
        self.check_existing_checkpoint()
        self.root.after(40, self.process_queue)
        
    def setup_styles(self):
        style = ttk.Style()
        style.theme_use('clam')
        
        style.configure(".", background=self.c_bg, foreground=self.c_text, font=("Segoe UI", 10))
        
        style.configure("Treeview",
                        background=self.c_card,
                        foreground=self.c_text,
                        fieldbackground=self.c_card,
                        rowheight=26,
                        bordercolor=self.c_card_border,
                        borderwidth=0)
        style.map("Treeview",
                  background=[("selected", self.c_accent)],
                  foreground=[("selected", "#ffffff")])
        style.configure("Treeview.Heading",
                        background="#262638",
                        foreground=self.c_text,
                        relief="flat",
                        font=("Segoe UI", 10, "bold"),
                        padding=6)
        style.map("Treeview.Heading", background=[("active", "#36364d")])
        
        style.configure("Custom.Horizontal.TProgressbar",
                        background=self.c_accent,
                        troughcolor=self.c_card,
                        bordercolor=self.c_card_border)
        
    def build_ui(self):
        # Header
        header = tk.Frame(self.root, bg=self.c_bg, padx=20, pady=10)
        header.pack(fill=tk.X)
        
        lbl_title = tk.Label(header, text="🛡️ ExFatRescue v1.3.0", font=("Segoe UI", 18, "bold"), fg=self.c_text, bg=self.c_bg)
        lbl_title.pack(side=tk.LEFT)
        
        lbl_sub = tk.Label(header, text="  Raw exFAT File, Project & Visual Map Recovery Engine", font=("Segoe UI", 10), fg=self.c_text_dim, bg=self.c_bg)
        lbl_sub.pack(side=tk.LEFT, pady=4)
        
        btn_donate = tk.Button(header, text="💖 Donate / Sponsor", command=self.show_donation_dialog, bg=self.c_donate, fg="#ffffff", font=("Segoe UI", 9, "bold"), relief=tk.FLAT, padx=12, pady=4, cursor="hand2")
        btn_donate.pack(side=tk.RIGHT, padx=4)
        
        btn_gh = tk.Button(header, text="⭐ GitHub", command=lambda: webbrowser.open("https://github.com/Kl3tus/ExFatRescue"), bg="#2d2d40", fg=self.c_text, font=("Segoe UI", 9), relief=tk.FLAT, padx=10, pady=4, cursor="hand2")
        btn_gh.pack(side=tk.RIGHT, padx=4)
        
        # Configuration Card
        config_card = tk.Frame(self.root, bg=self.c_card, bd=1, relief=tk.SOLID, padx=16, pady=10)
        config_card.pack(fill=tk.X, padx=20, pady=(0, 8))
        
        # Row 1
        r1 = tk.Frame(config_card, bg=self.c_card)
        r1.pack(fill=tk.X, pady=3)
        
        tk.Label(r1, text="Target Device:", font=("Segoe UI", 10, "bold"), fg=self.c_text, bg=self.c_card, width=13, anchor="w").pack(side=tk.LEFT)
        ent_drive = tk.Entry(r1, textvariable=self.target_drive_var, bg="#14141e", fg=self.c_text, insertbackground=self.c_text, width=24, relief=tk.FLAT, font=("Segoe UI", 10))
        ent_drive.pack(side=tk.LEFT, padx=(0, 20))
        
        tk.Label(r1, text="Output Directory:", font=("Segoe UI", 10, "bold"), fg=self.c_text, bg=self.c_card, width=14, anchor="w").pack(side=tk.LEFT)
        ent_dest = tk.Entry(r1, textvariable=self.output_dir_var, bg="#14141e", fg=self.c_text, insertbackground=self.c_text, relief=tk.FLAT, font=("Segoe UI", 10))
        ent_dest.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        
        btn_browse = tk.Button(r1, text="Browse...", command=self.browse_destination, bg="#2d2d40", fg=self.c_text, relief=tk.FLAT, padx=10, activebackground="#3d3d54", activeforeground=self.c_text)
        btn_browse.pack(side=tk.RIGHT)
        
        # Row 2
        r2 = tk.Frame(config_card, bg=self.c_card)
        r2.pack(fill=tk.X, pady=(8, 2))
        
        self.btn_scan = tk.Button(r2, text="🔍 Start Deep Scan", command=self.start_scan, bg=self.c_accent, fg="#ffffff", font=("Segoe UI", 10, "bold"), relief=tk.FLAT, padx=16, pady=5, cursor="hand2")
        self.btn_scan.pack(side=tk.LEFT, padx=(0, 8))
        
        self.btn_stop = tk.Button(r2, text="⏹ Stop", command=self.stop_action, bg="#333348", fg=self.c_text_dim, relief=tk.FLAT, padx=14, pady=5, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=(0, 8))
        
        self.btn_resume = tk.Button(r2, text="⚡ Resume Last Scan", command=self.resume_scan, bg="#059669", fg="#ffffff", font=("Segoe UI", 10, "bold"), relief=tk.FLAT, padx=14, pady=5, state=tk.DISABLED)
        self.btn_resume.pack(side=tk.LEFT, padx=(0, 15))
        
        self.lbl_scan_info = tk.Label(r2, text="Auto-save enabled: progress backed up continuously to checkpoint.", fg=self.c_text_dim, bg=self.c_card, font=("Segoe UI", 9))
        self.lbl_scan_info.pack(side=tk.LEFT)

        # View Mode Selector Tabs
        view_tabs_frame = tk.Frame(self.root, bg=self.c_bg, padx=20, pady=2)
        view_tabs_frame.pack(fill=tk.X)
        
        self.btn_view_list = tk.Button(view_tabs_frame, text="📋 File List Table", command=lambda: self.switch_view("list"),
                                       bg=self.c_accent, fg="#ffffff", font=("Segoe UI", 9, "bold"), relief=tk.FLAT, padx=14, pady=5, cursor="hand2")
        self.btn_view_list.pack(side=tk.LEFT, padx=(0, 4))
        
        self.btn_view_map = tk.Button(view_tabs_frame, text="🗺️ Visual Map", command=lambda: self.switch_view("map"),
                                      bg="#222232", fg=self.c_text_dim, font=("Segoe UI", 9, "bold"), relief=tk.FLAT, padx=14, pady=5, cursor="hand2")
        self.btn_view_map.pack(side=tk.LEFT, padx=(0, 4))

        self.btn_view_tree = tk.Button(view_tabs_frame, text="📁 Folder Explorer", command=lambda: self.switch_view("tree"),
                                       bg="#222232", fg=self.c_text_dim, font=("Segoe UI", 9, "bold"), relief=tk.FLAT, padx=14, pady=5, cursor="hand2")
        self.btn_view_tree.pack(side=tk.LEFT)

        # Main Central Container
        self.center_container = tk.Frame(self.root, bg=self.c_bg)
        self.center_container.pack(fill=tk.BOTH, expand=True, padx=20, pady=4)
        
        # ---------------- VIEW 1: FILE LIST TABLE ----------------
        self.view_list_frame = tk.Frame(self.center_container, bg=self.c_bg)
        
        # Filter & Search Toolbar (Row 1: Type & Search)
        filter_bar_top = tk.Frame(self.view_list_frame, bg=self.c_bg, pady=2)
        filter_bar_top.pack(fill=tk.X)
        
        tk.Label(filter_bar_top, text="Display Filter:", fg=self.c_text, bg=self.c_bg, font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT, padx=(0, 6))
        for t_label, t_val in [("All Items", "all"), ("📁 Actual Folders Only", "folders_only"), ("📄 Files Only", "files_only")]:
            rb = tk.Radiobutton(filter_bar_top, text=t_label, value=t_val, variable=self.item_type_filter_var, command=self.filter_tree_display,
                                bg=self.c_bg, fg=self.c_text, selectcolor=self.c_card, activebackground=self.c_bg, activeforeground=self.c_accent, font=("Segoe UI", 9))
            rb.pack(side=tk.LEFT, padx=3)
            
        # Search Box
        tk.Label(filter_bar_top, text="Search:", fg=self.c_text, bg=self.c_bg, font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT, padx=(12, 4))
        ent_search = tk.Entry(filter_bar_top, textvariable=self.search_var, bg=self.c_card, fg=self.c_text, insertbackground=self.c_text, relief=tk.FLAT, width=16, font=("Segoe UI", 9))
        ent_search.pack(side=tk.LEFT)
        
        # Selection Controls in Toolbar
        btn_desel = tk.Button(filter_bar_top, text="Deselect All", command=self.deselect_all, bg="#2a2a3d", fg=self.c_text, relief=tk.FLAT, padx=7, font=("Segoe UI", 8))
        btn_desel.pack(side=tk.RIGHT)
        
        btn_sel_vis = tk.Button(filter_bar_top, text="Select Visible", command=self.select_all_visible, bg="#2a2a3d", fg=self.c_text, relief=tk.FLAT, padx=7, font=("Segoe UI", 8))
        btn_sel_vis.pack(side=tk.RIGHT, padx=3)
        
        btn_sel_cat = tk.Button(filter_bar_top, text="Select Current View", command=self.select_current_category, bg="#4f46e5", fg="#ffffff", relief=tk.FLAT, padx=8, font=("Segoe UI", 8, "bold"))
        btn_sel_cat.pack(side=tk.RIGHT, padx=3)
        
        # Prominent ⭐ Select ALL Button in Toolbar
        self.btn_sel_all = tk.Button(filter_bar_top, text="⭐ Select ALL Files", command=self.select_all_everything, bg="#2563eb", fg="#ffffff", relief=tk.FLAT, padx=12, pady=2, font=("Segoe UI", 9, "bold"), cursor="hand2")
        self.btn_sel_all.pack(side=tk.RIGHT, padx=4)

        # Filter Toolbar (Row 2: Category Filters)
        filter_bar_bot = tk.Frame(self.view_list_frame, bg=self.c_bg, pady=2)
        filter_bar_bot.pack(fill=tk.X)
        
        tk.Label(filter_bar_bot, text="Category:", fg=self.c_text_dim, bg=self.c_bg, font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT, padx=(0, 6))
        categories = ["All", "📁 Folder", "Godot / Game", "3D & Assets", "Code & Dev", "Archives", "Media", "Documents", "Other"]
        for cat in categories:
            rb = tk.Radiobutton(filter_bar_bot, text=cat, value=cat, variable=self.category_var, command=self.filter_tree_display,
                                bg=self.c_bg, fg=self.c_text, selectcolor=self.c_card, activebackground=self.c_bg, activeforeground=self.c_accent, font=("Segoe UI", 8))
            rb.pack(side=tk.LEFT, padx=2)

        # Center: Treeview Table
        table_frame = tk.Frame(self.view_list_frame, bg=self.c_card)
        table_frame.pack(fill=tk.BOTH, expand=True, pady=(2, 0))
        
        columns = ("selected", "name", "category", "size", "cluster", "type")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="extended")
        
        self.tree.heading("selected", text="✓", command=self.toggle_all_selection)
        self.tree.heading("name", text="File / Folder Name", command=lambda: self.sort_tree("name", False))
        self.tree.heading("category", text="Category", command=lambda: self.sort_tree("category", False))
        self.tree.heading("size", text="Size", command=lambda: self.sort_tree("size", False))
        self.tree.heading("cluster", text="First Cluster", command=lambda: self.sort_tree("cluster", False))
        self.tree.heading("type", text="Entry Type", command=lambda: self.sort_tree("type", False))
        
        self.tree.column("selected", width=40, anchor="center")
        self.tree.column("name", width=430, anchor="w")
        self.tree.column("category", width=130, anchor="center")
        self.tree.column("size", width=110, anchor="e")
        self.tree.column("cluster", width=110, anchor="center")
        self.tree.column("type", width=95, anchor="center")
        
        scrollbar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        self.tree.bind("<ButtonRelease-1>", self.on_tree_click)
        self.tree.bind("<space>", self.on_tree_space)
        
        self.view_list_frame.pack(fill=tk.BOTH, expand=True)

        # ---------------- VIEW 2: VISUAL MAP ----------------
        self.view_map_frame = tk.Frame(self.center_container, bg=self.c_card, bd=1, relief=tk.SOLID)
        
        # Map Toolbar
        map_bar = tk.Frame(self.view_map_frame, bg="#1a1a27", padx=12, pady=6)
        map_bar.pack(fill=tk.X)
        
        tk.Label(map_bar, text="🗺️ Visual Disk Map", font=("Segoe UI", 10, "bold"), fg=self.c_text, bg="#1a1a27").pack(side=tk.LEFT, padx=(0, 15))
        
        tk.Label(map_bar, text="Group By:", font=("Segoe UI", 9, "bold"), fg=self.c_text_dim, bg="#1a1a27").pack(side=tk.LEFT, padx=(0, 6))
        for g_opt in ["Actual Folders", "Category", "Extension (.ext)"]:
            tk.Radiobutton(map_bar, text=g_opt, value=g_opt, variable=self.map_group_var, command=self.render_visual_map,
                           bg="#1a1a27", fg=self.c_text, selectcolor="#2d2d42", font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=4)
                           
        self.btn_map_refresh = tk.Button(map_bar, text="🔄 Redraw Map", command=self.render_visual_map, bg="#2d2d42", fg=self.c_text, font=("Segoe UI", 8), relief=tk.FLAT, padx=8)
        self.btn_map_refresh.pack(side=tk.RIGHT, padx=4)
        
        self.btn_map_sel = tk.Button(map_bar, text="✓ Select Group", command=self.select_active_map_group, bg="#4f46e5", fg="#ffffff", font=("Segoe UI", 8, "bold"), relief=tk.FLAT, padx=10)
        self.btn_map_sel.pack(side=tk.RIGHT, padx=4)

        self.btn_map_filter = tk.Button(map_bar, text="🔍 Jump to Table", command=self.jump_map_to_table, bg="#0284c7", fg="#ffffff", font=("Segoe UI", 8, "bold"), relief=tk.FLAT, padx=10)
        self.btn_map_filter.pack(side=tk.RIGHT, padx=4)

        # Visual Map Canvas
        self.map_canvas = tk.Canvas(self.view_map_frame, bg="#11111a", highlightthickness=0)
        self.map_canvas.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self.map_canvas.bind("<Configure>", lambda e: self.render_visual_map())
        self.map_canvas.bind("<Motion>", self.on_map_hover)
        self.map_canvas.bind("<Button-1>", self.on_map_click)
        self.map_canvas.bind("<Double-Button-1>", lambda e: self.jump_map_to_table())
        
        # Map Hover Info Bar
        self.map_status_bar = tk.Frame(self.view_map_frame, bg="#181824", padx=12, pady=5)
        self.map_status_bar.pack(fill=tk.X)
        self.lbl_map_info = tk.Label(self.map_status_bar, text="Hover over or click a block to inspect size, file count, and folder.", fg=self.c_text_dim, bg="#181824", font=("Segoe UI", 9))
        self.lbl_map_info.pack(side=tk.LEFT)

        # ---------------- VIEW 3: FOLDER EXPLORER ----------------
        self.view_tree_frame = tk.Frame(self.center_container, bg=self.c_card, bd=1, relief=tk.SOLID)
        
        tree_toolbar = tk.Frame(self.view_tree_frame, bg="#1a1a27", padx=12, pady=6)
        tree_toolbar.pack(fill=tk.X)
        tk.Label(tree_toolbar, text="📁 Hierarchical Folder Explorer", font=("Segoe UI", 10, "bold"), fg=self.c_text, bg="#1a1a27").pack(side=tk.LEFT, padx=(0, 15))
        
        tk.Label(tree_toolbar, text="View Mode:", font=("Segoe UI", 9, "bold"), fg=self.c_text_dim, bg="#1a1a27").pack(side=tk.LEFT, padx=(0, 6))
        for m_label, m_val in [("📁 Actual Folders on Disk", "folders"), ("🏷️ Categorized Buckets", "categories")]:
            tk.Radiobutton(tree_toolbar, text=m_label, value=m_val, variable=self.folder_tree_mode_var, command=self.populate_folder_tree,
                           bg="#1a1a27", fg=self.c_text, selectcolor="#2d2d42", font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=4)

        btn_exp_all = tk.Button(tree_toolbar, text="Expand All", command=self.expand_folder_tree, bg="#2d2d42", fg=self.c_text, font=("Segoe UI", 8), relief=tk.FLAT, padx=8)
        btn_exp_all.pack(side=tk.RIGHT, padx=3)
        btn_col_all = tk.Button(tree_toolbar, text="Collapse All", command=self.collapse_folder_tree, bg="#2d2d42", fg=self.c_text, font=("Segoe UI", 8), relief=tk.FLAT, padx=8)
        btn_col_all.pack(side=tk.RIGHT, padx=3)
        
        folder_table_frame = tk.Frame(self.view_tree_frame, bg=self.c_card)
        folder_table_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        
        f_columns = ("selected", "type_info", "size", "count")
        self.folder_tree = ttk.Treeview(folder_table_frame, columns=f_columns, show="tree headings", selectmode="extended")
        
        self.folder_tree.heading("#0", text="Folder / Item Hierarchy")
        self.folder_tree.heading("selected", text="✓")
        self.folder_tree.heading("type_info", text="Type")
        self.folder_tree.heading("size", text="Total Size")
        self.folder_tree.heading("count", text="File Count")
        
        self.folder_tree.column("#0", width=520, anchor="w")
        self.folder_tree.column("selected", width=40, anchor="center")
        self.folder_tree.column("type_info", width=140, anchor="center")
        self.folder_tree.column("size", width=130, anchor="e")
        self.folder_tree.column("count", width=120, anchor="center")
        
        f_scroll = ttk.Scrollbar(folder_table_frame, orient=tk.VERTICAL, command=self.folder_tree.yview)
        self.folder_tree.configure(yscrollcommand=f_scroll.set)
        self.folder_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        f_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.folder_tree.bind("<ButtonRelease-1>", self.on_folder_tree_click)

        # ---------------- FOOTER WITH TWO RECOVERY BUTTONS ----------------
        footer = tk.Frame(self.root, bg=self.c_card, padx=20, pady=10)
        footer.pack(fill=tk.X)
        
        f_top = tk.Frame(footer, bg=self.c_card)
        f_top.pack(fill=tk.X, pady=(0, 6))
        
        self.progress_bar = ttk.Progressbar(f_top, style="Custom.Horizontal.TProgressbar", mode="determinate")
        self.progress_bar.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 15))
        
        self.lbl_selected_summary = tk.Label(f_top, text="0 files selected (0 B)", fg=self.c_text, bg=self.c_card, font=("Segoe UI", 10, "bold"))
        self.lbl_selected_summary.pack(side=tk.RIGHT)
        
        f_bot = tk.Frame(footer, bg=self.c_card)
        f_bot.pack(fill=tk.X)
        
        self.lbl_footer_status = tk.Label(f_bot, text="Ready to scan.", fg=self.c_text_dim, bg=self.c_card, font=("Segoe UI", 9))
        self.lbl_footer_status.pack(side=tk.LEFT)
        
        # Two Recovery Buttons side-by-side:
        self.btn_recover_selected = tk.Button(
            f_bot,
            text="🚀 Recover Selected Files",
            command=self.start_recovery,
            bg=self.c_success,
            fg="#ffffff",
            font=("Segoe UI", 11, "bold"),
            relief=tk.FLAT,
            padx=18,
            pady=6,
            cursor="hand2"
        )
        self.btn_recover_selected.pack(side=tk.RIGHT)
        
        self.btn_recover_all = tk.Button(
            f_bot,
            text="📦 Recover ALL Files (Entire Drive)",
            command=self.start_recovery_all,
            bg="#0284c7",
            fg="#ffffff",
            font=("Segoe UI", 11, "bold"),
            relief=tk.FLAT,
            padx=18,
            pady=6,
            cursor="hand2"
        )
        self.btn_recover_all.pack(side=tk.RIGHT, padx=(0, 12))

    # ==========================================================================
    # Folder Resolution Engine (Actual Folders vs Categorized)
    # ==========================================================================

    def refresh_folder_cache(self):
        """ Rebuilds index of actual directory entries discovered on disk """
        dirs = [i for i in self.discovered_items if i.get('is_dir')]
        self.cached_dir_names = set(d['name'] for d in dirs)
        self.sorted_dir_items = sorted(dirs, key=lambda x: x['first_cluster'])

    def resolve_actual_folder(self, item):
        """ Resolves which actual folder a file belongs to based on disk structures """
        if item.get('is_dir'):
            return item['name']
            
        name = item['name']
        # 1. Explicit path in record
        if '/' in name or '\\' in name:
            parts = [p for p in name.replace('\\', '/').split('/') if p]
            if len(parts) > 1:
                return parts[0]
                
        # 2. Known subfolder keyword match
        name_lower = name.lower()
        for d_name in self.cached_dir_names:
            if d_name.lower() in name_lower and len(d_name) > 3:
                return d_name
                
        # 3. Cluster proximity (exFAT directory table clustering)
        if self.sorted_dir_items:
            c = item['first_cluster']
            idx = bisect.bisect_right([d['first_cluster'] for d in self.sorted_dir_items], c) - 1
            if 0 <= idx < len(self.sorted_dir_items):
                cand = self.sorted_dir_items[idx]
                if c - cand['first_cluster'] < 1500:
                    return cand['name']
                    
        return "[Root / Loose Files]"

    # ==========================================================================
    # View Switching (Table / Visual Map / Folder Tree)
    # ==========================================================================

    def switch_view(self, view_name):
        self.active_view = view_name
        self.view_list_frame.pack_forget()
        self.view_map_frame.pack_forget()
        self.view_tree_frame.pack_forget()
        
        self.btn_view_list.config(bg="#222232", fg=self.c_text_dim)
        self.btn_view_map.config(bg="#222232", fg=self.c_text_dim)
        self.btn_view_tree.config(bg="#222232", fg=self.c_text_dim)
        
        if view_name == "list":
            self.view_list_frame.pack(fill=tk.BOTH, expand=True)
            self.btn_view_list.config(bg=self.c_accent, fg="#ffffff")
        elif view_name == "map":
            self.view_map_frame.pack(fill=tk.BOTH, expand=True)
            self.btn_view_map.config(bg=self.c_accent, fg="#ffffff")
            self.root.after(30, self.render_visual_map)
        elif view_name == "tree":
            self.view_tree_frame.pack(fill=tk.BOTH, expand=True)
            self.btn_view_tree.config(bg=self.c_accent, fg="#ffffff")
            self.populate_folder_tree()

    # ==========================================================================
    # Interactive Visual Treemap Implementation
    # ==========================================================================

    def render_visual_map(self):
        if self.active_view != "map":
            return
            
        c_w = self.map_canvas.winfo_width()
        c_h = self.map_canvas.winfo_height()
        if c_w <= 20 or c_h <= 20:
            return
            
        self.map_canvas.delete("all")
        self.rendered_tiles = []
        
        if not self.discovered_items:
            self.map_canvas.create_text(
                c_w // 2, c_h // 2,
                text="No files discovered yet.\nClick 'Start Deep Scan' or 'Resume Last Scan' to map disk space.",
                fill=self.c_text_dim, font=("Segoe UI", 12), justify="center"
            )
            return

        self.refresh_folder_cache()
        group_mode = self.map_group_var.get()
        group_data = {}

        for idx, item in enumerate(self.discovered_items):
            if item['is_dir']:
                continue
            sz = max(item['data_len'], 1)
            cat = item.get('category', 'Other')
            
            if group_mode == "Actual Folders":
                key = self.resolve_actual_folder(item)
            elif group_mode == "Category":
                key = cat
            else: # Extension (.ext)
                ext = os.path.splitext(item['name'])[1].lower()
                key = ext if ext else "No Extension"
                    
            if key not in group_data:
                group_data[key] = {
                    'name': key,
                    'category': cat if group_mode != "Actual Folders" else "📁 Folder",
                    'size': 0,
                    'count': 0,
                    'indices': [],
                    'ext_breakdown': {}
                }
            group_data[key]['size'] += sz
            group_data[key]['count'] += 1
            group_data[key]['indices'].append(idx)
            
            ext = os.path.splitext(item['name'])[1].lower()
            group_data[key]['ext_breakdown'][ext] = group_data[key]['ext_breakdown'].get(ext, 0) + sz

        groups = list(group_data.values())
        total_bytes = sum(g['size'] for g in groups)
        if total_bytes <= 0:
            return

        # Squarify groups
        layout = squarify(groups, (4, 4, c_w - 8, c_h - 8))
        self.rendered_tiles = layout

        for tile in layout:
            rx, ry, rw, rh = tile['rect']
            cat = tile.get('category', 'Other')
            col = CATEGORY_COLORS.get(cat, CATEGORY_COLORS['Other'])
            
            # Outer block
            t_id = self.map_canvas.create_rectangle(
                rx, ry, rx + rw, ry + rh,
                fill=col['fill'], outline=col['border'], width=1, tags=("tile", tile['name'])
            )
            tile['canvas_id'] = t_id

            pct = (tile['size'] / total_bytes) * 100
            
            # Sub-blocks inside tile (nested category tiles)
            if rw > 90 and rh > 65 and len(tile.get('ext_breakdown', {})) > 1:
                inner_rect = (rx + 4, ry + 24, rw - 8, rh - 28)
                sub_children = [{'name': ext if ext else 'raw', 'size': s, 'cat': cat}
                                for ext, s in tile['ext_breakdown'].items() if s > 0]
                sub_layout = squarify(sub_children[:6], inner_rect)
                for st in sub_layout:
                    s_rx, s_ry, s_rw, s_rh = st['rect']
                    if s_rw > 20 and s_rh > 15:
                        self.map_canvas.create_rectangle(
                            s_rx, s_ry, s_rx + s_rw, s_ry + s_rh,
                            fill=col['subfill'], outline=col['border'], width=1
                        )
                        if s_rw > 45 and s_rh > 20:
                            self.map_canvas.create_text(
                                s_rx + 4, s_ry + 3,
                                text=st['name'], fill=col['accent'], font=("Segoe UI", 8), anchor="nw"
                            )

            # Header text
            if rw > 60 and rh > 24:
                title_str = tile['name']
                if len(title_str) > 28:
                    title_str = title_str[:25] + "..."
                label_text = f"{title_str} ({format_size(tile['size'])})"
                self.map_canvas.create_text(
                    rx + 6, ry + 5,
                    text=label_text, fill="#ffffff", font=("Segoe UI", 9, "bold"), anchor="nw"
                )
            if rw > 100 and rh > 45:
                sub_label = f"{tile['count']:,} files | {pct:.1f}%"
                self.map_canvas.create_text(
                    rx + 6, ry + 20,
                    text=sub_label, fill=col['accent'], font=("Segoe UI", 8), anchor="nw"
                )

    def on_map_hover(self, event):
        x, y = event.x, event.y
        hovered = None
        for tile in self.rendered_tiles:
            rx, ry, rw, rh = tile['rect']
            if rx <= x <= rx + rw and ry <= y <= ry + rh:
                hovered = tile
                break
                
        if hovered != self.hovered_tile:
            self.hovered_tile = hovered
            if hovered:
                total_bytes = sum(t['size'] for t in self.rendered_tiles)
                pct = (hovered['size'] / max(total_bytes, 1)) * 100
                self.lbl_map_info.config(
                    text=f"📁 {hovered['name']} — Size: {format_size(hovered['size'])} ({pct:.1f}% of data) | Files: {hovered['count']:,} | Type: {hovered.get('category')} [Double-click to view in table]"
                )
            else:
                self.lbl_map_info.config(text="Hover over or click a block to inspect size, file count, and folder.")

    def on_map_click(self, event):
        x, y = event.x, event.y
        for tile in self.rendered_tiles:
            rx, ry, rw, rh = tile['rect']
            if rx <= x <= rx + rw and ry <= y <= ry + rh:
                self.active_tile_data = tile
                # Highlight active tile
                self.map_canvas.delete("active_highlight")
                self.map_canvas.create_rectangle(
                    rx, ry, rx + rw, ry + rh,
                    outline="#ffffff", width=2, tags="active_highlight"
                )
                break

    def jump_map_to_table(self):
        if not self.active_tile_data and self.hovered_tile:
            self.active_tile_data = self.hovered_tile
            
        if not self.active_tile_data:
            messagebox.showinfo("Select Block", "Please click on a block in the visual map first.")
            return
            
        group_mode = self.map_group_var.get()
        name = self.active_tile_data['name']
        
        self.switch_view("list")
        self.item_type_filter_var.set("all")
        if group_mode == "Category":
            if name in ["Godot / Game", "3D & Assets", "Code & Dev", "Archives", "Media", "Documents"]:
                self.category_var.set(name)
            else:
                self.category_var.set("All")
            self.search_var.set("")
        elif group_mode == "Extension (.ext)":
            self.category_var.set("All")
            self.search_var.set(name)
        else: # Actual Folders
            self.category_var.set("All")
            if name != "[Root / Loose Files]":
                self.search_var.set(name)
            else:
                self.search_var.set("")
        self.filter_tree_display()

    def select_active_map_group(self):
        if not self.active_tile_data and self.hovered_tile:
            self.active_tile_data = self.hovered_tile
            
        if not self.active_tile_data:
            messagebox.showinfo("Select Block", "Please click on a block in the map to select it.")
            return
            
        indices = self.active_tile_data['indices']
        drive_path = self.target_drive_var.get().strip()
        max_size = get_physical_drive_size(drive_path)
        max_clusters = max_size // CLUSTER_SIZE
        
        for idx in indices:
            if idx not in self.selected_item_indices:
                it = self.discovered_items[idx]
                if not it['is_dir'] and 0 < it['data_len'] <= max_size and 2 <= it['first_cluster'] < max_clusters:
                    self.selected_item_indices.add(idx)
                    self.selected_count += 1
                    self.selected_bytes += it['data_len']
                    
        self.update_selection_summary_fast()
        messagebox.showinfo("Selected", f"Selected {len(indices):,} files in '{self.active_tile_data['name']}' ({format_size(self.active_tile_data['size'])})!")

    # ==========================================================================
    # Hierarchical Folder Tree Implementation (Actual Folders vs Categories)
    # ==========================================================================

    def populate_folder_tree(self):
        self.folder_tree.delete(*self.folder_tree.get_children())
        if not self.discovered_items:
            return

        self.refresh_folder_cache()
        mode = self.folder_tree_mode_var.get()
        
        if mode == "folders":
            # Mode A: ACTUAL DISCOVERED FOLDERS
            folder_groups = {}
            for idx, item in enumerate(self.discovered_items):
                if item['is_dir']:
                    continue
                fld = self.resolve_actual_folder(item)
                if fld not in folder_groups:
                    folder_groups[fld] = {'size': 0, 'count': 0, 'indices': []}
                folder_groups[fld]['size'] += item['data_len']
                folder_groups[fld]['count'] += 1
                folder_groups[fld]['indices'].append(idx)

            # Insert known empty folders as well
            for d in self.sorted_dir_items:
                if d['name'] not in folder_groups:
                    folder_groups[d['name']] = {'size': 0, 'count': 0, 'indices': []}

            for fld, data in sorted(folder_groups.items(), key=lambda x: x[1]['size'], reverse=True):
                node_id = f"fld_{fld}"
                checked = "[✓]" if (data['indices'] and all(i in self.selected_item_indices for i in data['indices'])) else "[ ]"
                self.folder_tree.insert(
                    "", tk.END, iid=node_id, text=f"📁 {fld}",
                    values=(checked, "Actual Folder", format_size(data['size']), f"{data['count']:,}")
                )
                for i in data['indices'][:35]:
                    it = self.discovered_items[i]
                    item_node = f"item_{i}"
                    i_checked = "[✓]" if i in self.selected_item_indices else "[ ]"
                    i_size = "-" if it['is_dir'] else format_size(it['data_len'])
                    self.folder_tree.insert(
                        node_id, tk.END, iid=item_node, text=it['name'],
                        values=(i_checked, it.get('category', 'File'), i_size, "1")
                    )
                if len(data['indices']) > 35:
                    self.folder_tree.insert(
                        node_id, tk.END, iid=f"more_{fld}", text=f"... and {len(data['indices']) - 35:,} more files ...",
                        values=("-", "-", "-", "-")
                    )
        else:
            # Mode B: CATEGORIZED BUCKETS
            cat_groups = {}
            for idx, item in enumerate(self.discovered_items):
                cat = item.get('category', 'Other')
                if cat not in cat_groups:
                    cat_groups[cat] = {'size': 0, 'count': 0, 'indices': []}
                cat_groups[cat]['size'] += item['data_len']
                cat_groups[cat]['count'] += 1
                cat_groups[cat]['indices'].append(idx)

            for cat, data in sorted(cat_groups.items(), key=lambda x: x[1]['size'], reverse=True):
                node_id = f"cat_{cat}"
                checked = "[✓]" if all(i in self.selected_item_indices for i in data['indices']) else "[ ]"
                self.folder_tree.insert(
                    "", tk.END, iid=node_id, text=f"🏷️ {cat}",
                    values=(checked, "Category Bucket", format_size(data['size']), f"{data['count']:,}")
                )
                for i in data['indices'][:30]:
                    it = self.discovered_items[i]
                    item_node = f"item_{i}"
                    i_checked = "[✓]" if i in self.selected_item_indices else "[ ]"
                    i_size = "-" if it['is_dir'] else format_size(it['data_len'])
                    self.folder_tree.insert(
                        node_id, tk.END, iid=item_node, text=it['name'],
                        values=(i_checked, cat, i_size, "1")
                    )
                if len(data['indices']) > 30:
                    self.folder_tree.insert(
                        node_id, tk.END, iid=f"more_{cat}", text=f"... and {len(data['indices']) - 30:,} more files ...",
                        values=("-", "-", "-", "-")
                    )

    def on_folder_tree_click(self, event):
        region = self.folder_tree.identify_region(event.x, event.y)
        if region in ("cell", "tree"):
            item_id = self.folder_tree.identify_row(event.y)
            if not item_id:
                return
            if item_id.startswith("fld_"):
                fld_name = item_id.replace("fld_", "")
                self.select_folder_group(fld_name)
                self.populate_folder_tree()
            elif item_id.startswith("cat_"):
                cat = item_id.replace("cat_", "")
                self.category_var.set(cat)
                self.select_current_category()
                self.populate_folder_tree()
            elif item_id.startswith("item_"):
                idx = int(item_id.replace("item_", ""))
                self.toggle_item_selection(str(idx))
                self.populate_folder_tree()

    def select_folder_group(self, fld_name):
        drive_path = self.target_drive_var.get().strip()
        max_size = get_physical_drive_size(drive_path)
        max_clusters = max_size // CLUSTER_SIZE
        
        self.refresh_folder_cache()
        indices = [idx for idx, it in enumerate(self.discovered_items)
                   if not it['is_dir'] and self.resolve_actual_folder(it) == fld_name]
        
        all_selected = all(i in self.selected_item_indices for i in indices) if indices else False
        for idx in indices:
            it = self.discovered_items[idx]
            if 0 < it['data_len'] <= max_size and 2 <= it['first_cluster'] < max_clusters:
                if all_selected:
                    if idx in self.selected_item_indices:
                        self.selected_item_indices.remove(idx)
                        self.selected_count -= 1
                        self.selected_bytes -= it['data_len']
                else:
                    if idx not in self.selected_item_indices:
                        self.selected_item_indices.add(idx)
                        self.selected_count += 1
                        self.selected_bytes += it['data_len']
        self.update_selection_summary_fast()

    def expand_folder_tree(self):
        for item in self.folder_tree.get_children():
            self.folder_tree.item(item, open=True)

    def collapse_folder_tree(self):
        for item in self.folder_tree.get_children():
            self.folder_tree.item(item, open=False)

    # ==========================================================================
    # Checkpoint Persistence (Crash Protection & Recovery)
    # ==========================================================================

    def get_checkpoint_path(self):
        dest = self.output_dir_var.get().strip()
        os.makedirs(dest, exist_ok=True)
        return os.path.join(dest, "exfat_rescue_checkpoint.json")

    def check_existing_checkpoint(self):
        cp_path = self.get_checkpoint_path()
        if os.path.exists(cp_path):
            try:
                with open(cp_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    items_count = len(data.get("items", []))
                    last_gb = data.get("current_offset", 0) / (1024**3)
                    self.resume_offset = data.get("current_offset")
                    if items_count > 0:
                        self.btn_resume.config(state=tk.NORMAL)
                        self.lbl_scan_info.config(text=f"⚡ Checkpoint ready: {items_count:,} files/folders at {last_gb:.1f} GB. Click 'Resume Last Scan' to continue!")
            except Exception:
                pass

    def save_checkpoint_atomic(self, current_offset, items_list):
        cp_path = self.get_checkpoint_path()
        try:
            temp_path = cp_path + ".tmp"
            bak_path = cp_path + ".bak"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump({
                    "current_offset": current_offset,
                    "timestamp": time.time(),
                    "items": items_list
                }, f)
            if os.path.exists(cp_path):
                try:
                    os.replace(cp_path, bak_path)
                except Exception:
                    pass
            os.replace(temp_path, cp_path)
        except Exception:
            pass

    def load_checkpoint(self):
        cp_path = self.get_checkpoint_path()
        if not os.path.exists(cp_path):
            bak_path = cp_path + ".bak"
            if os.path.exists(bak_path):
                cp_path = bak_path
            else:
                return None
        try:
            with open(cp_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    # ==========================================================================
    # High-Performance Selection & Filtering
    # ==========================================================================

    def on_search_change(self, *args):
        if self.search_debounce_timer:
            self.root.after_cancel(self.search_debounce_timer)
        self.search_debounce_timer = self.root.after(150, self.filter_tree_display)

    def show_donation_dialog(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("💖 Support ExFatRescue")
        dialog.geometry("380x430")
        dialog.configure(bg="#141824")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        tk.Label(dialog, text="💖 Support Open Source", bg="#141824", fg="#f43f5e", font=("Segoe UI", 14, "bold")).pack(pady=(16, 4))
        tk.Label(dialog, text="ExFatRescue is 100% free and open-source.\nIf it saved your projects, donations are deeply appreciated!", bg="#141824", fg="#94a3b8", font=("Segoe UI", 9), justify="center").pack(padx=20)

        qr_frame = tk.Frame(dialog, bg="#1e2538", bd=1, relief="solid")
        qr_frame.pack(pady=12, padx=30, fill="x")

        qr_path = get_resource_path("paypal_qr.png")
        if os.path.exists(qr_path) and HAS_PIL:
            try:
                qr_pil = Image.open(qr_path).resize((180, 180), Image.Resampling.LANCZOS)
                qr_photo = ImageTk.PhotoImage(qr_pil)
                lbl_qr = tk.Label(qr_frame, image=qr_photo, bg="#1e2538")
                lbl_qr.image = qr_photo
                lbl_qr.pack(pady=10)
            except Exception:
                tk.Label(qr_frame, text="[PayPal QR Code]", bg="#1e2538", fg="#cbd5e1").pack(pady=20)
        else:
            tk.Label(qr_frame, text="[PayPal QR Code]", bg="#1e2538", fg="#cbd5e1").pack(pady=20)

        tk.Label(qr_frame, text="Scan with PayPal app or camera", bg="#1e2538", fg="#f1f5f9", font=("Segoe UI", 10, "bold")).pack()
        
        btn_pp = tk.Button(qr_frame, text="🔗 Open PayPal (paypal.me/Kl3tus)", bg="#0284c7", fg="#ffffff", font=("Segoe UI", 10, "bold"), relief="flat", padx=12, pady=5, cursor="hand2",
                           command=lambda: webbrowser.open("https://paypal.me/Kl3tus"))
        btn_pp.pack(pady=(8, 14))

    def browse_destination(self):
        chosen = filedialog.askdirectory(initialdir=self.output_dir_var.get())
        if chosen:
            self.output_dir_var.set(chosen)
            self.check_existing_checkpoint()
            
    def on_tree_click(self, event):
        region = self.tree.identify_region(event.x, event.y)
        if region == "cell":
            col = self.tree.identify_column(event.x)
            item_id = self.tree.identify_row(event.y)
            if item_id and col == "#1":
                self.toggle_item_selection(item_id)
                    
    def on_tree_space(self, event):
        selected = self.tree.selection()
        for item_id in selected:
            self.toggle_item_selection(item_id)
            
    def toggle_item_selection(self, item_id):
        if item_id == "overflow_notice":
            return
        idx = int(item_id)
        if idx >= len(self.discovered_items):
            return
        item = self.discovered_items[idx]
        
        if idx in self.selected_item_indices:
            self.selected_item_indices.remove(idx)
            checked = "[ ]"
            if not item['is_dir']:
                self.selected_count -= 1
                self.selected_bytes -= item['data_len']
        else:
            self.selected_item_indices.add(idx)
            checked = "[✓]"
            if not item['is_dir']:
                self.selected_count += 1
                self.selected_bytes += item['data_len']
            
        values = list(self.tree.item(item_id, "values"))
        values[0] = checked
        self.tree.item(item_id, values=values)
        self.update_selection_summary_fast()

    def select_all_everything(self):
        """ Selects ALL valid files across the entire indexed dataset """
        drive_path = self.target_drive_var.get().strip()
        max_size = get_physical_drive_size(drive_path)
        max_clusters = max_size // CLUSTER_SIZE
        
        self.selected_item_indices.clear()
        self.selected_count = 0
        self.selected_bytes = 0
        
        for idx, item in enumerate(self.discovered_items):
            if not item['is_dir'] and 0 < item['data_len'] <= max_size and 2 <= item['first_cluster'] < max_clusters:
                self.selected_item_indices.add(idx)
                self.selected_count += 1
                self.selected_bytes += item['data_len']
                
        for item_id in self.tree.get_children():
            if item_id != "overflow_notice":
                values = list(self.tree.item(item_id, "values"))
                values[0] = "[✓]"
                self.tree.item(item_id, values=values)
                
        self.update_selection_summary_fast()
        messagebox.showinfo("Selected", f"Selected ALL {self.selected_count:,} files ({format_size(self.selected_bytes)} total) across the entire drive!")

    def select_current_category(self):
        """ Selects all files belonging to the active view/filter """
        drive_path = self.target_drive_var.get().strip()
        max_size = get_physical_drive_size(drive_path)
        max_clusters = max_size // CLUSTER_SIZE
        
        cat_filter = self.category_var.get()
        type_filter = self.item_type_filter_var.get()
        query = self.search_var.get().lower().strip()
        
        for idx, item in enumerate(self.discovered_items):
            if item['is_dir']:
                continue
            if type_filter == "folders_only":
                continue
            if cat_filter != "All" and item.get('category') != cat_filter:
                continue
            if query and query not in item['name'].lower():
                continue
                
            if idx not in self.selected_item_indices:
                if 0 < item['data_len'] <= max_size and 2 <= item['first_cluster'] < max_clusters:
                    self.selected_item_indices.add(idx)
                    self.selected_count += 1
                    self.selected_bytes += item['data_len']
                        
        for item_id in self.tree.get_children():
            if item_id != "overflow_notice":
                idx = int(item_id)
                if idx in self.selected_item_indices:
                    values = list(self.tree.item(item_id, "values"))
                    values[0] = "[✓]"
                    self.tree.item(item_id, values=values)
                    
        self.update_selection_summary_fast()

    def select_all_visible(self):
        drive_path = self.target_drive_var.get().strip()
        max_size = get_physical_drive_size(drive_path)
        max_clusters = max_size // CLUSTER_SIZE
        
        for item_id in self.tree.get_children():
            if item_id == "overflow_notice":
                continue
            idx = int(item_id)
            if idx not in self.selected_item_indices:
                item = self.discovered_items[idx]
                if not item['is_dir'] and 0 < item['data_len'] <= max_size and 2 <= item['first_cluster'] < max_clusters:
                    self.selected_item_indices.add(idx)
                    self.selected_count += 1
                    self.selected_bytes += item['data_len']
                values = list(self.tree.item(item_id, "values"))
                values[0] = "[✓]"
                self.tree.item(item_id, values=values)
        self.update_selection_summary_fast()
        
    def deselect_all(self):
        self.selected_item_indices.clear()
        self.selected_count = 0
        self.selected_bytes = 0
        for item_id in self.tree.get_children():
            if item_id != "overflow_notice":
                values = list(self.tree.item(item_id, "values"))
                values[0] = "[ ]"
                self.tree.item(item_id, values=values)
        self.update_selection_summary_fast()
        
    def toggle_all_selection(self):
        if len(self.selected_item_indices) >= len(self.discovered_items):
            self.deselect_all()
        else:
            self.select_all_everything()
            
    def update_selection_summary_fast(self):
        self.lbl_selected_summary.config(text=f"{self.selected_count:,} files selected ({format_size(max(self.selected_bytes, 0))})")
        
    def filter_tree_display(self, *args):
        query = self.search_var.get().lower().strip()
        cat_filter = self.category_var.get()
        type_filter = self.item_type_filter_var.get()
        
        self.tree.delete(*self.tree.get_children())
        
        displayed = 0
        for idx, item in enumerate(self.discovered_items):
            # Type filter: all, folders_only, files_only
            if type_filter == "folders_only" and not item['is_dir']:
                continue
            if type_filter == "files_only" and item['is_dir']:
                continue
                
            # Category filter
            if cat_filter != "All":
                if cat_filter == "📁 Folder" and not item['is_dir']:
                    continue
                if cat_filter != "📁 Folder" and item.get('category') != cat_filter:
                    continue
                    
            # Search filter
            if query and query not in item['name'].lower():
                continue
                
            checked = "[✓]" if idx in self.selected_item_indices else "[ ]"
            type_str = "Folder" if item['is_dir'] else "File"
            size_str = "-" if item['is_dir'] else format_size(item['data_len'])
            cat_display = "📁 Folder" if item['is_dir'] else item.get('category', 'Other')
            
            self.tree.insert("", tk.END, iid=str(idx), values=(
                checked,
                item['name'],
                cat_display,
                size_str,
                f"Cluster {item['first_cluster']}",
                type_str
            ))
            displayed += 1
            if displayed >= 2500:
                self.tree.insert("", tk.END, iid="overflow_notice", values=(
                    "-", f"... and {len(self.discovered_items) - 2500:,} more items (Use Search/Filters to narrow) ...",
                    "", "", "", ""
                ))
                break
                
    def sort_tree(self, col, reverse):
        if col == "name":
            self.discovered_items.sort(key=lambda x: x['name'].lower(), reverse=reverse)
        elif col == "size":
            self.discovered_items.sort(key=lambda x: x['data_len'], reverse=reverse)
        elif col == "cluster":
            self.discovered_items.sort(key=lambda x: x['first_cluster'], reverse=reverse)
        elif col == "category":
            self.discovered_items.sort(key=lambda x: x.get('category', 'Other'), reverse=reverse)
            
        self.filter_tree_display()
        self.tree.heading(col, command=lambda: self.sort_tree(col, not reverse))

    # ==========================================================================
    # Robust Background Scanning & Recovery
    # ==========================================================================
    
    def start_scan(self):
        if self.is_scanning or self.is_recovering:
            return
            
        drive_path = self.target_drive_var.get().strip()
        self.discovered_items.clear()
        self.selected_item_indices.clear()
        self.selected_count = 0
        self.selected_bytes = 0
        self.tree.delete(*self.tree.get_children())
        self.update_selection_summary_fast()
        
        self.is_scanning = True
        self.stop_requested = False
        self.btn_scan.config(state=tk.DISABLED, bg="#333348")
        self.btn_resume.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL, bg=self.c_danger, fg="#ffffff")
        self.btn_recover_selected.config(state=tk.DISABLED)
        self.btn_recover_all.config(state=tk.DISABLED)
        
        start_offset = 11 * 1024 * 1024 * 1024  # 11 GB (past Win11 installer)
        threading.Thread(target=self.scan_worker, args=(drive_path, start_offset, []), daemon=True).start()
        
    def resume_scan(self):
        if self.is_scanning or self.is_recovering:
            return
            
        cp = self.load_checkpoint()
        if not cp:
            messagebox.showinfo("Info", "No valid checkpoint found to resume.")
            return
            
        drive_path = self.target_drive_var.get().strip()
        max_size = get_physical_drive_size(drive_path)
        max_clusters = max_size // CLUSTER_SIZE
        
        self.discovered_items = cp.get("items", [])
        self.selected_item_indices.clear()
        self.selected_count = 0
        self.selected_bytes = 0
        
        for idx, item in enumerate(self.discovered_items):
            # Auto-select game projects, developer repos, and assets
            if item.get('category') in ['Godot / Game', 'Code & Dev', '3D & Assets']:
                if 0 < item['data_len'] <= max_size and 2 <= item['first_cluster'] < max_clusters:
                    self.selected_item_indices.add(idx)
                    if not item['is_dir']:
                        self.selected_count += 1
                        self.selected_bytes += item['data_len']
                    
        self.refresh_folder_cache()
        self.filter_tree_display()
        self.update_selection_summary_fast()
        
        start_offset = cp.get("current_offset", 11 * 1024 * 1024 * 1024)
        if start_offset >= max_size:
            messagebox.showinfo("Completed", "This scan previously finished all sectors! All files are loaded and ready to recover.")
            self.btn_recover_selected.config(state=tk.NORMAL)
            self.btn_recover_all.config(state=tk.NORMAL)
            return

        self.is_scanning = True
        self.stop_requested = False
        self.btn_scan.config(state=tk.DISABLED, bg="#333348")
        self.btn_resume.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL, bg=self.c_danger, fg="#ffffff")
        self.btn_recover_selected.config(state=tk.DISABLED)
        self.btn_recover_all.config(state=tk.DISABLED)
        
        threading.Thread(target=self.scan_worker, args=(drive_path, start_offset, list(self.discovered_items)), daemon=True).start()
        
    def stop_action(self):
        self.stop_requested = True
        self.btn_stop.config(state=tk.DISABLED, bg="#333348", fg=self.c_text_dim)
        self.lbl_footer_status.config(text="Stopping gracefully and saving checkpoint...")
        
    def scan_worker(self, drive_path, start_offset, initial_items):
        start_time = time.time()
        max_drive_size = get_physical_drive_size(drive_path)
        chunk_size = 16 * 1024 * 1024  # 16 MB chunk
        
        try:
            drive = open(drive_path, "rb")
        except PermissionError:
            self.msg_queue.put(("error", "Permission Denied: Run as Administrator to access physical drives!"))
            self.msg_queue.put(("scan_stopped", start_offset))
            return
        except Exception as e:
            self.msg_queue.put(("error", f"Could not open drive: {e}"))
            self.msg_queue.put(("scan_stopped", start_offset))
            return
            
        try:
            drive.seek(start_offset)
        except Exception:
            start_offset = 11 * 1024 * 1024 * 1024
            drive.seek(start_offset)
            
        current = start_offset
        worker_items = list(initial_items)
        seen_keys = set((item['name'], item['first_cluster'], item['data_len']) for item in worker_items)
        
        last_checkpoint_time = time.time()
        batch_items = []
        
        try:
            while current < max_drive_size and not self.stop_requested:
                to_read = min(chunk_size, max_drive_size - current)
                if to_read <= 0:
                    break
                    
                try:
                    buf = drive.read(to_read)
                except (OSError, IOError):
                    if current + chunk_size >= max_drive_size:
                        break  # Natural end of disk
                    current += 65536
                    try:
                        drive.seek(current)
                    except Exception:
                        break
                    continue
                    
                if not buf:
                    break  # Natural EOF
                    
                idx = 0
                while idx != -1:
                    if self.stop_requested:
                        break
                    idx = buf.find(b'\x85', idx)
                    if idx == -1:
                        break
                    if idx % 32 == 0:
                        entry = parse_exfat_entry_set(buf, idx, max_drive_size)
                        if entry:
                            key = (entry['name'], entry['first_cluster'], entry['data_len'])
                            if key not in seen_keys:
                                seen_keys.add(key)
                                entry['category'] = get_file_category(entry['name'], entry['is_dir'])
                                batch_items.append(entry)
                                worker_items.append(entry)
                    idx += 1
                    
                current += len(buf)
                
                # Dispatch batch updates to UI
                if len(batch_items) >= 100 or (batch_items and time.time() - last_checkpoint_time > 0.3):
                    self.msg_queue.put(("items_batch", batch_items))
                    batch_items = []
                    
                # Continuous auto-save checkpoint
                if time.time() - last_checkpoint_time > 5.0:
                    self.save_checkpoint_atomic(current, worker_items)
                    last_checkpoint_time = time.time()
                    
                progress = min(100.0, (current - 11 * 1024 * 1024 * 1024) / max(max_drive_size - 11 * 1024 * 1024 * 1024, 1) * 100)
                speed = ((current - start_offset) / (1024**2)) / max(time.time() - start_time, 0.001)
                self.msg_queue.put(("scan_progress", (progress, current, max_drive_size, speed)))
                
        except Exception as e:
            self.msg_queue.put(("error", f"Scan error: {e}"))
        finally:
            if batch_items:
                self.msg_queue.put(("items_batch", batch_items))
            self.save_checkpoint_atomic(current, worker_items)
            try:
                drive.close()
            except Exception:
                pass
                
            if self.stop_requested:
                self.msg_queue.put(("scan_stopped", current))
            else:
                self.msg_queue.put(("scan_done", current))

    # ==========================================================================
    # Recovery Execution (Preserves Discovered Folder Structure)
    # ==========================================================================

    def start_recovery(self):
        """ Recovers currently selected files into their actual folder directories """
        if not self.selected_item_indices:
            messagebox.showinfo("Select Files", "Please select at least one file to recover, or click 'Recover ALL Files'.")
            return
            
        dest_dir = self.output_dir_var.get().strip()
        if not dest_dir:
            messagebox.showerror("Error", "Please specify an output directory.")
            return
            
        drive_path = self.target_drive_var.get().strip()
        
        self.is_recovering = True
        self.stop_requested = False
        self.btn_scan.config(state=tk.DISABLED)
        self.btn_resume.config(state=tk.DISABLED)
        self.btn_recover_selected.config(state=tk.DISABLED)
        self.btn_recover_all.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL, bg=self.c_danger, fg="#ffffff")
        
        indices = list(self.selected_item_indices)
        threading.Thread(target=self.recovery_worker, args=(drive_path, dest_dir, indices), daemon=True).start()

    def start_recovery_all(self):
        """ 1-Click Recovery for ALL files across the entire drive into actual folders """
        if self.is_scanning or self.is_recovering:
            return
            
        dest_dir = self.output_dir_var.get().strip()
        if not dest_dir:
            messagebox.showerror("Error", "Please specify an output directory first.")
            return
            
        if not self.discovered_items:
            messagebox.showinfo("No Files", "No files discovered yet. Please run a scan first or click 'Resume Last Scan'.")
            return
            
        drive_path = self.target_drive_var.get().strip()
        max_size = get_physical_drive_size(drive_path)
        max_clusters = max_size // CLUSTER_SIZE
        
        all_valid_indices = []
        total_bytes = 0
        for idx, item in enumerate(self.discovered_items):
            if not item['is_dir'] and 0 < item['data_len'] <= max_size and 2 <= item['first_cluster'] < max_clusters:
                all_valid_indices.append(idx)
                total_bytes += item['data_len']
                
        if not all_valid_indices:
            messagebox.showinfo("Notice", "No valid files found within drive bounds.")
            return
            
        confirm = messagebox.askyesno(
            "Confirm Full Recovery",
            f"Ready to recover ALL discovered files across the entire drive!\n\n"
            f"• Total Files: {len(all_valid_indices):,}\n"
            f"• Total Size: {format_size(total_bytes)}\n"
            f"• Target Device: {drive_path}\n"
            f"• Destination: {dest_dir}\n\n"
            f"Files will be organized into their discovered actual folders.\n"
            f"Proceed with full batch recovery?"
        )
        if not confirm:
            return
            
        self.selected_item_indices = set(all_valid_indices)
        self.selected_count = len(all_valid_indices)
        self.selected_bytes = total_bytes
        self.update_selection_summary_fast()
        
        self.is_recovering = True
        self.stop_requested = False
        self.btn_scan.config(state=tk.DISABLED)
        self.btn_resume.config(state=tk.DISABLED)
        self.btn_recover_selected.config(state=tk.DISABLED)
        self.btn_recover_all.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL, bg=self.c_danger, fg="#ffffff")
        
        threading.Thread(target=self.recovery_worker, args=(drive_path, dest_dir, all_valid_indices), daemon=True).start()

    def recovery_worker(self, drive_path, dest_dir, indices):
        os.makedirs(dest_dir, exist_ok=True)
        max_drive_size = get_physical_drive_size(drive_path)
        max_clusters = max_drive_size // CLUSTER_SIZE
        
        self.refresh_folder_cache()
        
        try:
            drive = open(drive_path, "rb")
        except Exception as e:
            self.msg_queue.put(("error", f"Could not open drive: {e}"))
            self.msg_queue.put(("recovery_done", 0))
            return
            
        recovered_count = 0
        total = len(indices)
        last_progress_time = time.time()
        
        try:
            for i, idx in enumerate(indices):
                if self.stop_requested:
                    break
                if idx >= len(self.discovered_items):
                    continue
                item = self.discovered_items[idx]
                if item['is_dir']:
                    continue
                if item['data_len'] <= 0 or item['data_len'] > max_drive_size or item['first_cluster'] < 2 or item['first_cluster'] >= max_clusters:
                    continue
                    
                folder_name = self.resolve_actual_folder(item)
                clean_name = sanitize_filename(item['name'])
                offset = CLUSTER_HEAP_OFFSET + (item['first_cluster'] - 2) * CLUSTER_SIZE
                
                if offset + item['data_len'] > max_drive_size:
                    continue
                
                # Organize files into actual folders on disk
                if folder_name and folder_name != "[Root / Loose Files]":
                    target_folder = os.path.join(dest_dir, sanitize_filename(folder_name))
                else:
                    target_folder = dest_dir
                    
                os.makedirs(target_folder, exist_ok=True)
                out_path = os.path.join(target_folder, clean_name)
                
                # Collision handling
                if os.path.exists(out_path):
                    dir_name = os.path.dirname(out_path)
                    base_name = os.path.basename(out_path)
                    stem, ext = os.path.splitext(base_name)
                    counter = 1
                    while os.path.exists(out_path):
                        out_path = os.path.join(dir_name, f"{stem}_{item['first_cluster']}_{counter}{ext}")
                        counter += 1
                        
                now = time.time()
                if now - last_progress_time > 0.1 or i == total - 1:
                    self.msg_queue.put(("recovery_progress", (i + 1, total, os.path.basename(out_path))))
                    last_progress_time = now
                
                try:
                    drive.seek(offset)
                    with open(out_path, "wb") as f_out:
                        remaining = item['data_len']
                        while remaining > 0 and not self.stop_requested:
                            chunk = drive.read(min(remaining, 1024 * 1024))
                            if not chunk:
                                break
                            f_out.write(chunk)
                            remaining -= len(chunk)
                    recovered_count += 1
                except Exception:
                    pass
        finally:
            drive.close()
            self.msg_queue.put(("recovery_done", recovered_count))
            
    # ==========================================================================
    # High-Performance UI Queue Dispatcher (Non-Blocking)
    # ==========================================================================
    
    def process_queue(self):
        processed = 0
        try:
            while processed < 100:
                msg_type, data = self.msg_queue.get_nowait()
                processed += 1
                
                if msg_type == "items_batch":
                    cat_filter = self.category_var.get()
                    type_filter = self.item_type_filter_var.get()
                    query = self.search_var.get().lower().strip()
                    drive_path = self.target_drive_var.get().strip()
                    max_size = get_physical_drive_size(drive_path)
                    max_clusters = max_size // CLUSTER_SIZE
                    
                    for item in data:
                        idx = len(self.discovered_items)
                        self.discovered_items.append(item)
                        
                        if item.get('category') in ['Godot / Game', 'Code & Dev', '3D & Assets']:
                            if 0 < item['data_len'] <= max_size and 2 <= item['first_cluster'] < max_clusters:
                                self.selected_item_indices.add(idx)
                                if not item['is_dir']:
                                    self.selected_count += 1
                                    self.selected_bytes += item['data_len']
                                
                        if len(self.tree.get_children()) < 2000:
                            # Apply filters
                            if type_filter == "folders_only" and not item['is_dir']:
                                continue
                            if type_filter == "files_only" and item['is_dir']:
                                continue
                            if cat_filter != "All":
                                if cat_filter == "📁 Folder" and not item['is_dir']:
                                    continue
                                if cat_filter != "📁 Folder" and item.get('category') != cat_filter:
                                    continue
                            if query and query not in item['name'].lower():
                                continue
                                
                            checked = "[✓]" if idx in self.selected_item_indices else "[ ]"
                            type_str = "Folder" if item['is_dir'] else "File"
                            size_str = "-" if item['is_dir'] else format_size(item['data_len'])
                            cat_display = "📁 Folder" if item['is_dir'] else item.get('category', 'Other')
                            
                            self.tree.insert("", tk.END, iid=str(idx), values=(
                                checked, item['name'], cat_display, size_str, f"Cluster {item['first_cluster']}", type_str
                            ))
                    self.update_selection_summary_fast()
                    
                elif msg_type == "scan_progress":
                    pct, curr_bytes, total_bytes, speed = data
                    self.progress_bar['value'] = pct
                    gb_done = curr_bytes / (1024**3)
                    gb_tot = total_bytes / (1024**3)
                    self.lbl_footer_status.config(text=f"Scanning: {gb_done:.1f}/{gb_tot:.1f} GB ({pct:.1f}%) | Speed: {speed:.1f} MB/s | Discovered: {len(self.discovered_items):,} files")
                    
                elif msg_type in ("scan_done", "scan_stopped"):
                    self.is_scanning = False
                    self.btn_scan.config(state=tk.NORMAL, bg=self.c_accent)
                    self.btn_stop.config(state=tk.DISABLED, bg="#333348", fg=self.c_text_dim)
                    self.btn_recover_selected.config(state=tk.NORMAL)
                    self.btn_recover_all.config(state=tk.NORMAL)
                    self.btn_resume.config(state=tk.NORMAL)
                    
                    self.refresh_folder_cache()
                    
                    if msg_type == "scan_done":
                        self.progress_bar['value'] = 100
                        self.lbl_footer_status.config(text=f"Scan complete! Discovered {len(self.discovered_items):,} items ({len(self.cached_dir_names)} actual folders). Click '📦 Recover ALL Files' or inspect folders.")
                    else:
                        self.lbl_footer_status.config(text=f"Scan paused/stopped. Checkpoint saved with {len(self.discovered_items):,} items. Click 'Resume' anytime or 'Recover' now.")
                    
                    if self.active_view == "map":
                        self.render_visual_map()
                    elif self.active_view == "tree":
                        self.populate_folder_tree()
                        
                elif msg_type == "recovery_progress":
                    curr, total, name = data
                    pct = (curr / max(total, 1)) * 100
                    self.progress_bar['value'] = pct
                    self.lbl_footer_status.config(text=f"Recovering ({curr:,}/{total:,}): {name}...")
                    
                elif msg_type == "recovery_done":
                    self.is_recovering = False
                    self.btn_scan.config(state=tk.NORMAL)
                    self.btn_resume.config(state=tk.NORMAL)
                    self.btn_stop.config(state=tk.DISABLED, bg="#333348", fg=self.c_text_dim)
                    self.btn_recover_selected.config(state=tk.NORMAL)
                    self.btn_recover_all.config(state=tk.NORMAL)
                    self.progress_bar['value'] = 100
                    self.lbl_footer_status.config(text=f"Recovery finished! Saved {data:,} files into actual folders.")
                    messagebox.showinfo("Success", f"Recovery complete!\n\nSuccessfully recovered {data:,} files organized into their actual folders to:\n{self.output_dir_var.get()}")
                    
                elif msg_type == "error":
                    self.lbl_footer_status.config(text=f"Notice: {data}")
                    
        except queue.Empty:
            pass
            
        self.root.after(40, self.process_queue)

def main():
    root = tk.Tk()
    app = ExFatRescueApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()
