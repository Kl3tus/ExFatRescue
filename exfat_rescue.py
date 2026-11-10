import os
import sys
import struct
import time
import re
import json
import threading
import queue
import webbrowser
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# ==============================================================================
# ExFatRescue v1.0.1 - High-Performance exFAT Recovery with Auto-Save & Resume
# Special support for Godot projects, source code, 3D assets, and media
# Author: Mikel Bryan (Kl3tus)
# ==============================================================================

def get_resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath(os.path.dirname(__file__)), relative_path)

CATEGORY_MAP = {
    'Godot / Game': ['.godot', '.gd', '.tscn', '.tres', '.import', '.gdextension', '.shader'],
    '3D & Assets': ['.blend', '.blend1', '.fbx', '.obj', '.gltf', '.glb', '.dae', '.psd'],
    'Code & Dev': ['.java', '.py', '.cpp', '.c', '.h', '.hpp', '.cs', '.kt', '.rs', '.js', '.ts', '.html', '.css', '.json', '.xml', '.yaml', '.yml', '.gradle'],
    'Archives': ['.zip', '.7z', '.rar', '.tar', '.gz', '.bz2', '.xz', '.iso'],
    'Media': ['.png', '.jpg', '.jpeg', '.webp', '.svg', '.gif', '.bmp', '.mp4', '.mkv', '.avi', '.mov', '.mp3', '.wav', '.ogg', '.flac'],
    'Documents': ['.pdf', '.txt', '.md', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.csv', '.rtf']
}

def format_size(bytes_val):
    if bytes_val < 1024:
        return f"{bytes_val} B"
    elif bytes_val < 1024 * 1024:
        return f"{bytes_val / 1024:.1f} KB"
    elif bytes_val < 1024 * 1024 * 1024:
        return f"{bytes_val / (1024 * 1024):.1f} MB"
    else:
        return f"{bytes_val / (1024 * 1024 * 1024):.2f} GB"

def get_file_category(filename):
    ext = os.path.splitext(filename)[1].lower()
    for cat, exts in CATEGORY_MAP.items():
        if ext in exts:
            return cat
    return 'Other'

def sanitize_filename(name):
    clean = re.sub(r'[\x00\\/*?:"<>|]', '_', name).strip()
    return clean if clean else "unnamed_file"

def parse_exfat_entry_set(buf, offset):
    """
    Parses an exFAT directory entry set (0x85 File + 0xC0 Stream + 0xC1 Name)
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
    
    if first_cluster < 2:
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
# Modern High-Performance GUI
# ==============================================================================

class ExFatRescueApp:
    def __init__(self, root):
        self.root = root
        self.root.title("ExFatRescue - exFAT Lost File & Project Recovery")
        self.root.geometry("1120x750")
        self.root.minsize(920, 620)
        
        # Color palette
        self.c_bg = "#181824"
        self.c_card = "#222232"
        self.c_card_border = "#2e2e42"
        self.c_accent = "#3d7bfd"
        self.c_accent_hover = "#5c91ff"
        self.c_success = "#10b981"
        self.c_danger = "#ef4444"
        self.c_donate = "#e11d48"
        self.c_text = "#f3f4f6"
        self.c_text_dim = "#9ca3af"
        
        self.root.configure(bg=self.c_bg)
        
        # State variables
        self.discovered_items = []  # list of dicts
        self.selected_item_indices = set()
        self.selected_count = 0     # O(1) tracking
        self.selected_bytes = 0     # O(1) tracking
        
        self.is_scanning = False
        self.is_recovering = False
        self.stop_requested = False
        self.resume_offset = None
        self.msg_queue = queue.Queue()
        
        self.target_drive_var = tk.StringVar(value=r"\\.\PhysicalDrive1")
        self.output_dir_var = tk.StringVar(value=r"H:\SD Card\ExFatRescue_Output")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", self.on_search_change)
        self.category_var = tk.StringVar(value="All")
        
        self.search_debounce_timer = None
        
        self.setup_styles()
        self.build_ui()
        
        # Check for existing checkpoint
        self.check_existing_checkpoint()
        
        # Polling queue for thread-safe UI updates
        self.root.after(50, self.process_queue)
        
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
                        background="#2a2a3d",
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
        header = tk.Frame(self.root, bg=self.c_bg, padx=20, pady=12)
        header.pack(fill=tk.X)
        
        lbl_title = tk.Label(header, text="🛡️ ExFatRescue", font=("Segoe UI", 18, "bold"), fg=self.c_text, bg=self.c_bg)
        lbl_title.pack(side=tk.LEFT)
        
        lbl_sub = tk.Label(header, text="  Raw exFAT Project & File Recovery", font=("Segoe UI", 11), fg=self.c_text_dim, bg=self.c_bg)
        lbl_sub.pack(side=tk.LEFT, pady=4)
        
        btn_donate = tk.Button(header, text="💖 Donate / Sponsor", command=self.show_donation_dialog, bg=self.c_donate, fg="#ffffff", font=("Segoe UI", 9, "bold"), relief=tk.FLAT, padx=12, pady=4, cursor="hand2")
        btn_donate.pack(side=tk.RIGHT, padx=4)
        
        btn_gh = tk.Button(header, text="⭐ GitHub", command=lambda: webbrowser.open("https://github.com/Kl3tus/ExFatRescue"), bg="#2d2d40", fg=self.c_text, font=("Segoe UI", 9), relief=tk.FLAT, padx=10, pady=4, cursor="hand2")
        btn_gh.pack(side=tk.RIGHT, padx=4)
        
        # Configuration Card
        config_card = tk.Frame(self.root, bg=self.c_card, bd=1, relief=tk.SOLID, padx=16, pady=12)
        config_card.pack(fill=tk.X, padx=20, pady=(0, 10))
        
        # Row 1
        r1 = tk.Frame(config_card, bg=self.c_card)
        r1.pack(fill=tk.X, pady=3)
        
        tk.Label(r1, text="Target Device:", font=("Segoe UI", 10, "bold"), fg=self.c_text, bg=self.c_card, width=13, anchor="w").pack(side=tk.LEFT)
        ent_drive = tk.Entry(r1, textvariable=self.target_drive_var, bg="#1a1a26", fg=self.c_text, insertbackground=self.c_text, width=24, relief=tk.FLAT, font=("Segoe UI", 10))
        ent_drive.pack(side=tk.LEFT, padx=(0, 20))
        
        tk.Label(r1, text="Output Directory:", font=("Segoe UI", 10, "bold"), fg=self.c_text, bg=self.c_card, width=14, anchor="w").pack(side=tk.LEFT)
        ent_dest = tk.Entry(r1, textvariable=self.output_dir_var, bg="#1a1a26", fg=self.c_text, insertbackground=self.c_text, relief=tk.FLAT, font=("Segoe UI", 10))
        ent_dest.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        
        btn_browse = tk.Button(r1, text="Browse...", command=self.browse_destination, bg="#2d2d40", fg=self.c_text, relief=tk.FLAT, padx=10, activebackground="#3d3d54", activeforeground=self.c_text)
        btn_browse.pack(side=tk.RIGHT)
        
        # Row 2
        r2 = tk.Frame(config_card, bg=self.c_card)
        r2.pack(fill=tk.X, pady=(10, 2))
        
        self.btn_scan = tk.Button(r2, text="🔍 Start Deep Scan", command=self.start_scan, bg=self.c_accent, fg="#ffffff", font=("Segoe UI", 10, "bold"), relief=tk.FLAT, padx=16, pady=5, cursor="hand2")
        self.btn_scan.pack(side=tk.LEFT, padx=(0, 8))
        
        self.btn_stop = tk.Button(r2, text="⏹ Stop", command=self.stop_action, bg="#333348", fg=self.c_text_dim, relief=tk.FLAT, padx=14, pady=5, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=(0, 8))
        
        self.btn_resume = tk.Button(r2, text="⚡ Resume Last Scan", command=self.resume_scan, bg="#059669", fg="#ffffff", font=("Segoe UI", 10, "bold"), relief=tk.FLAT, padx=14, pady=5, state=tk.DISABLED)
        self.btn_resume.pack(side=tk.LEFT)
        
        self.lbl_scan_info = tk.Label(r2, text="Ready. Automatic checkpointing enabled: progress is saved every 500 files.", fg=self.c_text_dim, bg=self.c_card, font=("Segoe UI", 9))
        self.lbl_scan_info.pack(side=tk.LEFT, padx=15)
        
        # Filter & Search Toolbar
        filter_bar = tk.Frame(self.root, bg=self.c_bg, padx=20, pady=4)
        filter_bar.pack(fill=tk.X)
        
        tk.Label(filter_bar, text="Filter Category:", fg=self.c_text, bg=self.c_bg, font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT, padx=(0, 8))
        
        categories = ["All", "Godot / Game", "3D & Assets", "Code & Dev", "Archives", "Media", "Documents"]
        for cat in categories:
            rb = tk.Radiobutton(filter_bar, text=cat, value=cat, variable=self.category_var, command=self.filter_tree_display,
                                bg=self.c_bg, fg=self.c_text, selectcolor=self.c_card, activebackground=self.c_bg, activeforeground=self.c_accent, font=("Segoe UI", 9))
            rb.pack(side=tk.LEFT, padx=4)
            
        # Search Box
        tk.Label(filter_bar, text="Search:", fg=self.c_text, bg=self.c_bg, font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT, padx=(20, 6))
        ent_search = tk.Entry(filter_bar, textvariable=self.search_var, bg=self.c_card, fg=self.c_text, insertbackground=self.c_text, relief=tk.FLAT, width=20, font=("Segoe UI", 9))
        ent_search.pack(side=tk.LEFT)
        
        # Selection Buttons
        btn_desel = tk.Button(filter_bar, text="Deselect All", command=self.deselect_all, bg="#2a2a3d", fg=self.c_text, relief=tk.FLAT, padx=8, font=("Segoe UI", 8))
        btn_desel.pack(side=tk.RIGHT)
        btn_sel = tk.Button(filter_bar, text="Select All Visible", command=self.select_all_visible, bg="#2a2a3d", fg=self.c_text, relief=tk.FLAT, padx=8, font=("Segoe UI", 8))
        btn_sel.pack(side=tk.RIGHT, padx=6)
        
        # Center: Treeview Table
        table_frame = tk.Frame(self.root, bg=self.c_card, padx=20, pady=4)
        table_frame.pack(fill=tk.BOTH, expand=True)
        
        columns = ("selected", "name", "category", "size", "cluster", "type")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="extended")
        
        self.tree.heading("selected", text="✓", command=self.toggle_all_selection)
        self.tree.heading("name", text="File Name", command=lambda: self.sort_tree("name", False))
        self.tree.heading("category", text="Category", command=lambda: self.sort_tree("category", False))
        self.tree.heading("size", text="Size", command=lambda: self.sort_tree("size", False))
        self.tree.heading("cluster", text="First Cluster", command=lambda: self.sort_tree("cluster", False))
        self.tree.heading("type", text="Entry Type", command=lambda: self.sort_tree("type", False))
        
        self.tree.column("selected", width=40, anchor="center")
        self.tree.column("name", width=380, anchor="w")
        self.tree.column("category", width=130, anchor="center")
        self.tree.column("size", width=110, anchor="e")
        self.tree.column("cluster", width=110, anchor="center")
        self.tree.column("type", width=100, anchor="center")
        
        scrollbar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        self.tree.bind("<ButtonRelease-1>", self.on_tree_click)
        self.tree.bind("<space>", self.on_tree_space)
        
        # Footer
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
        
        self.btn_recover = tk.Button(f_bot, text="🚀 Recover Selected Files", command=self.start_recovery, bg=self.c_success, fg="#ffffff", font=("Segoe UI", 11, "bold"), relief=tk.FLAT, padx=20, pady=6, cursor="hand2")
        self.btn_recover.pack(side=tk.RIGHT)

    # ==========================================================================
    # Checkpoint Persistence (Crash Protection & Resume)
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
                    self.btn_resume.config(state=tk.NORMAL)
                    self.lbl_scan_info.config(text=f"⚡ Found previous scan checkpoint: {items_count} files at {last_gb:.1f} GB. Click 'Resume Last Scan' to continue!")
            except Exception:
                pass

    def save_checkpoint(self, current_offset):
        cp_path = self.get_checkpoint_path()
        try:
            temp_path = cp_path + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump({
                    "current_offset": current_offset,
                    "timestamp": time.time(),
                    "items": self.discovered_items
                }, f)
            os.replace(temp_path, cp_path)
        except Exception:
            pass

    def load_checkpoint(self):
        cp_path = self.get_checkpoint_path()
        if not os.path.exists(cp_path):
            return None
        try:
            with open(cp_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            return None

    # ==========================================================================
    # Event Handlers & High Performance Selection
    # ==========================================================================

    def on_search_change(self, *args):
        # Debounce search by 150ms so typing stays instantaneous
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
        
    def select_all_visible(self):
        for item_id in self.tree.get_children():
            idx = int(item_id)
            if idx not in self.selected_item_indices:
                self.selected_item_indices.add(idx)
                item = self.discovered_items[idx]
                if not item['is_dir']:
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
            values = list(self.tree.item(item_id, "values"))
            values[0] = "[ ]"
            self.tree.item(item_id, values=values)
        self.update_selection_summary_fast()
        
    def toggle_all_selection(self):
        if len(self.selected_item_indices) >= len(self.tree.get_children()):
            self.deselect_all()
        else:
            self.select_all_visible()
            
    def update_selection_summary_fast(self):
        self.lbl_selected_summary.config(text=f"{self.selected_count} files selected ({format_size(max(self.selected_bytes, 0))})")
        
    def filter_tree_display(self, *args):
        query = self.search_var.get().lower().strip()
        cat_filter = self.category_var.get()
        
        self.tree.delete(*self.tree.get_children())
        
        # Display up to 2,000 matches to keep UI buttery smooth at 60fps
        displayed = 0
        for idx, item in enumerate(self.discovered_items):
            if cat_filter != "All" and item['category'] != cat_filter:
                continue
            if query and query not in item['name'].lower():
                continue
                
            checked = "[✓]" if idx in self.selected_item_indices else "[ ]"
            type_str = "Folder" if item['is_dir'] else "File"
            size_str = "-" if item['is_dir'] else format_size(item['data_len'])
            
            self.tree.insert("", tk.END, iid=str(idx), values=(
                checked,
                item['name'],
                item['category'],
                size_str,
                f"Cluster {item['first_cluster']}",
                type_str
            ))
            displayed += 1
            if displayed >= 2500:
                # Add notice row
                self.tree.insert("", tk.END, iid="overflow_notice", values=(
                    "-", f"... and {len(self.discovered_items) - 2500} more items (Use Search/Category to filter) ...",
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
            self.discovered_items.sort(key=lambda x: x['category'], reverse=reverse)
            
        self.filter_tree_display()
        self.tree.heading(col, command=lambda: self.sort_tree(col, not reverse))

    # ==========================================================================
    # Background Workers
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
        self.btn_recover.config(state=tk.DISABLED)
        
        start_offset = 11 * 1024 * 1024 * 1024  # 11 GB
        threading.Thread(target=self.scan_worker, args=(drive_path, start_offset), daemon=True).start()
        
    def resume_scan(self):
        if self.is_scanning or self.is_recovering:
            return
            
        cp = self.load_checkpoint()
        if not cp:
            messagebox.showinfo("Info", "No valid checkpoint found to resume.")
            return
            
        drive_path = self.target_drive_var.get().strip()
        self.discovered_items = cp.get("items", [])
        self.selected_item_indices.clear()
        self.selected_count = 0
        self.selected_bytes = 0
        
        for idx, item in enumerate(self.discovered_items):
            if item.get('category') in ['Godot / Game', 'Code & Dev', '3D & Assets']:
                self.selected_item_indices.add(idx)
                if not item['is_dir']:
                    self.selected_count += 1
                    self.selected_bytes += item['data_len']
                    
        self.filter_tree_display()
        self.update_selection_summary_fast()
        
        start_offset = cp.get("current_offset", 11 * 1024 * 1024 * 1024)
        self.is_scanning = True
        self.stop_requested = False
        self.btn_scan.config(state=tk.DISABLED, bg="#333348")
        self.btn_resume.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL, bg=self.c_danger, fg="#ffffff")
        self.btn_recover.config(state=tk.DISABLED)
        
        threading.Thread(target=self.scan_worker, args=(drive_path, start_offset), daemon=True).start()
        
    def stop_action(self):
        self.stop_requested = True
        self.lbl_footer_status.config(text="Stopping gracefully and saving checkpoint...")
        
    def scan_worker(self, drive_path, start_offset):
        start_time = time.time()
        end_offset   = 511 * 1024 * 1024 * 1024 # 511 GB
        chunk_size   = 16 * 1024 * 1024         # 16 MB
        
        try:
            drive = open(drive_path, "rb")
        except PermissionError:
            self.msg_queue.put(("error", "Permission Denied: Run as Administrator to access physical drives!"))
            self.msg_queue.put(("scan_done", False))
            return
        except Exception as e:
            self.msg_queue.put(("error", f"Could not open drive: {e}"))
            self.msg_queue.put(("scan_done", False))
            return
            
        drive.seek(start_offset)
        current = start_offset
        seen_keys = set((item['name'], item['first_cluster'], item['data_len']) for item in self.discovered_items)
        
        last_checkpoint_time = time.time()
        batch_items = []
        
        try:
            while current < end_offset and not self.stop_requested:
                to_read = min(chunk_size, end_offset - current)
                try:
                    buf = drive.read(to_read)
                except OSError:
                    # Skip bad sectors gracefully
                    current += 65536
                    drive.seek(current)
                    continue
                    
                if not buf:
                    break
                    
                idx = 0
                while True:
                    idx = buf.find(b'\x85', idx)
                    if idx == -1:
                        break
                    if idx % 32 == 0:
                        entry = parse_exfat_entry_set(buf, idx)
                        if entry:
                            key = (entry['name'], entry['first_cluster'], entry['data_len'])
                            if key not in seen_keys:
                                seen_keys.add(key)
                                entry['category'] = get_file_category(entry['name'])
                                batch_items.append(entry)
                    idx += 1
                    
                current += len(buf)
                
                # Batch dispatch items every 250ms or when batch exceeds 100
                if len(batch_items) >= 100 or (batch_items and time.time() - last_checkpoint_time > 0.25):
                    self.msg_queue.put(("items_batch", batch_items))
                    batch_items = []
                    
                # Auto-save checkpoint every 5 seconds
                if time.time() - last_checkpoint_time > 5.0:
                    self.save_checkpoint(current)
                    last_checkpoint_time = time.time()
                    
                progress = (current - 11 * 1024 * 1024 * 1024) / (end_offset - 11 * 1024 * 1024 * 1024) * 100
                speed = ((current - start_offset) / (1024**2)) / max(time.time() - start_time, 0.001)
                self.msg_queue.put(("scan_progress", (progress, current, speed)))
                
        except Exception as e:
            self.msg_queue.put(("error", f"Scan error: {e}"))
        finally:
            if batch_items:
                self.msg_queue.put(("items_batch", batch_items))
            self.save_checkpoint(current)
            drive.close()
            self.msg_queue.put(("scan_done", current))
            
    def start_recovery(self):
        if not self.selected_item_indices:
            messagebox.showinfo("Select Files", "Please select at least one file to recover.")
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
        self.btn_recover.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL, bg=self.c_danger, fg="#ffffff")
        
        indices = list(self.selected_item_indices)
        threading.Thread(target=self.recovery_worker, args=(drive_path, dest_dir, indices), daemon=True).start()
        
    def recovery_worker(self, drive_path, dest_dir, indices):
        os.makedirs(dest_dir, exist_ok=True)
        
        CLUSTER_SIZE = 128 * 1024 # 128 KB
        CLUSTER_HEAP_OFFSET = 32 * 1024 * 1024 # 32 MB
        
        try:
            drive = open(drive_path, "rb")
        except Exception as e:
            self.msg_queue.put(("error", f"Could not open drive: {e}"))
            self.msg_queue.put(("recovery_done", False))
            return
            
        recovered_count = 0
        total = len(indices)
        
        try:
            for i, idx in enumerate(indices):
                if self.stop_requested:
                    break
                if idx >= len(self.discovered_items):
                    continue
                item = self.discovered_items[idx]
                if item['is_dir']:
                    continue
                    
                clean_name = sanitize_filename(item['name'])
                offset = CLUSTER_HEAP_OFFSET + (item['first_cluster'] - 2) * CLUSTER_SIZE
                
                out_path = os.path.join(dest_dir, clean_name)
                if os.path.exists(out_path):
                    out_path = os.path.join(dest_dir, f"{item['first_cluster']}_{clean_name}")
                    
                self.msg_queue.put(("recovery_progress", (i + 1, total, clean_name)))
                
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
            while processed < 100:  # Bound processing to 100 events per frame for buttery UI
                msg_type, data = self.msg_queue.get_nowait()
                processed += 1
                
                if msg_type == "items_batch":
                    cat_filter = self.category_var.get()
                    query = self.search_var.get().lower().strip()
                    
                    for item in data:
                        idx = len(self.discovered_items)
                        self.discovered_items.append(item)
                        
                        # Auto-select high value developer files
                        if item['category'] in ['Godot / Game', 'Code & Dev', '3D & Assets']:
                            self.selected_item_indices.add(idx)
                            if not item['is_dir']:
                                self.selected_count += 1
                                self.selected_bytes += item['data_len']
                                
                        # Insert into treeview if under 2,000 items and matches filter
                        if len(self.tree.get_children()) < 2000:
                            if (cat_filter == "All" or item['category'] == cat_filter) and (not query or query in item['name'].lower()):
                                checked = "[✓]" if idx in self.selected_item_indices else "[ ]"
                                type_str = "Folder" if item['is_dir'] else "File"
                                size_str = "-" if item['is_dir'] else format_size(item['data_len'])
                                self.tree.insert("", tk.END, iid=str(idx), values=(
                                    checked, item['name'], item['category'], size_str, f"Cluster {item['first_cluster']}", type_str
                                ))
                    self.update_selection_summary_fast()
                    
                elif msg_type == "scan_progress":
                    pct, curr_bytes, speed = data
                    self.progress_bar['value'] = pct
                    gb_done = curr_bytes / (1024**3)
                    self.lbl_footer_status.config(text=f"Scanning: {gb_done:.1f}/511 GB ({pct:.1f}%) | Speed: {speed:.1f} MB/s | Discovered: {len(self.discovered_items)} files (Saved)")
                    
                elif msg_type == "scan_done":
                    self.is_scanning = False
                    self.btn_scan.config(state=tk.NORMAL, bg=self.c_accent)
                    self.btn_stop.config(state=tk.DISABLED, bg="#333348", fg=self.c_text_dim)
                    self.btn_recover.config(state=tk.NORMAL)
                    self.btn_resume.config(state=tk.NORMAL)
                    self.progress_bar['value'] = 100
                    self.lbl_footer_status.config(text=f"Scan complete! Discovered {len(self.discovered_items)} files/folders. Select items above and click 'Recover'.")
                    
                elif msg_type == "recovery_progress":
                    curr, total, name = data
                    pct = (curr / max(total, 1)) * 100
                    self.progress_bar['value'] = pct
                    self.lbl_footer_status.config(text=f"Recovering ({curr}/{total}): {name}...")
                    
                elif msg_type == "recovery_done":
                    self.is_recovering = False
                    self.btn_scan.config(state=tk.NORMAL)
                    self.btn_resume.config(state=tk.NORMAL)
                    self.btn_stop.config(state=tk.DISABLED, bg="#333348", fg=self.c_text_dim)
                    self.btn_recover.config(state=tk.NORMAL)
                    self.progress_bar['value'] = 100
                    self.lbl_footer_status.config(text=f"Recovery finished! Saved {data} files to destination.")
                    messagebox.showinfo("Success", f"Recovery complete! Successfully recovered {data} files to:\n{self.output_dir_var.get()}")
                    
                elif msg_type == "error":
                    messagebox.showerror("Notice", data)
                    
        except queue.Empty:
            pass
            
        self.root.after(40, self.process_queue)

def main():
    root = tk.Tk()
    app = ExFatRescueApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()
