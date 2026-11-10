import os
import sys
import struct
import time
import re
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
# ExFatRescue - Open Source exFAT Raw File & Project Recovery Tool
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
    
    # Reject strings with non-printable characters
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
# Modern GUI Implementation
# ==============================================================================

class ExFatRescueApp:
    def __init__(self, root):
        self.root = root
        self.root.title("ExFatRescue - exFAT Lost File & Project Recovery")
        self.root.geometry("1120x740")
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
        self.is_scanning = False
        self.is_recovering = False
        self.stop_requested = False
        self.msg_queue = queue.Queue()
        
        self.target_drive_var = tk.StringVar(value=r"\\.\PhysicalDrive1")
        self.output_dir_var = tk.StringVar(value=r"H:\SD Card\ExFatRescue_Output")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", self.filter_tree_display)
        self.category_var = tk.StringVar(value="All")
        
        self.setup_styles()
        self.build_ui()
        
        # Polling queue for thread-safe UI updates
        self.root.after(100, self.process_queue)
        
    def setup_styles(self):
        style = ttk.Style()
        style.theme_use('clam')
        
        style.configure(".", background=self.c_bg, foreground=self.c_text, font=("Segoe UI", 10))
        
        # Treeview styling
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
        
        # Progress bar
        style.configure("Custom.Horizontal.TProgressbar",
                        background=self.c_accent,
                        troughcolor=self.c_card,
                        bordercolor=self.c_card_border)
        
    def build_ui(self):
        # Top Header
        header = tk.Frame(self.root, bg=self.c_bg, padx=20, pady=12)
        header.pack(fill=tk.X)
        
        lbl_title = tk.Label(header, text="🛡️ ExFatRescue", font=("Segoe UI", 18, "bold"), fg=self.c_text, bg=self.c_bg)
        lbl_title.pack(side=tk.LEFT)
        
        lbl_sub = tk.Label(header, text="  Raw exFAT Lost Project & File Extractor", font=("Segoe UI", 11), fg=self.c_text_dim, bg=self.c_bg)
        lbl_sub.pack(side=tk.LEFT, pady=4)
        
        # Header Right: Donate & GitHub buttons
        btn_donate = tk.Button(header, text="💖 Donate / Sponsor", command=self.show_donation_dialog, bg=self.c_donate, fg="#ffffff", font=("Segoe UI", 9, "bold"), relief=tk.FLAT, padx=12, pady=4, cursor="hand2")
        btn_donate.pack(side=tk.RIGHT, padx=4)
        
        btn_gh = tk.Button(header, text="⭐ GitHub", command=lambda: webbrowser.open("https://github.com/Kl3tus/ExFatRescue"), bg="#2d2d40", fg=self.c_text, font=("Segoe UI", 9), relief=tk.FLAT, padx=10, pady=4, cursor="hand2")
        btn_gh.pack(side=tk.RIGHT, padx=4)
        
        # Configuration Card
        config_card = tk.Frame(self.root, bg=self.c_card, bd=1, relief=tk.SOLID, padx=16, pady=12)
        config_card.pack(fill=tk.X, padx=20, pady=(0, 10))
        
        # Row 1: Target drive and Output path
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
        
        # Row 2: Scan Action Buttons
        r2 = tk.Frame(config_card, bg=self.c_card)
        r2.pack(fill=tk.X, pady=(10, 2))
        
        self.btn_scan = tk.Button(r2, text="🔍 Start Deep Scan", command=self.start_scan, bg=self.c_accent, fg="#ffffff", font=("Segoe UI", 10, "bold"), relief=tk.FLAT, padx=16, pady=5, cursor="hand2")
        self.btn_scan.pack(side=tk.LEFT, padx=(0, 8))
        
        self.btn_stop = tk.Button(r2, text="⏹ Stop", command=self.stop_action, bg="#333348", fg=self.c_text_dim, relief=tk.FLAT, padx=14, pady=5, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT)
        
        self.lbl_scan_info = tk.Label(r2, text="Ready. Click 'Start Deep Scan' to index files from the unallocated space.", fg=self.c_text_dim, bg=self.c_card, font=("Segoe UI", 9))
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
        
        # Bottom Status & Action Footer
        footer = tk.Frame(self.root, bg=self.c_card, padx=20, pady=10)
        footer.pack(fill=tk.X)
        
        # Progress and status line
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
    # Logic & Event Handlers
    # ==========================================================================
    
    def show_donation_dialog(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("💖 Support ExFatRescue")
        dialog.geometry("380x420")
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
            
    def on_tree_click(self, event):
        region = self.tree.identify_region(event.x, event.y)
        if region == "cell":
            col = self.tree.identify_column(event.x)
            item_id = self.tree.identify_row(event.y)
            if item_id:
                if col == "#1":  # Clicked checkmark column
                    self.toggle_item_selection(item_id)
                    
    def on_tree_space(self, event):
        selected = self.tree.selection()
        for item_id in selected:
            self.toggle_item_selection(item_id)
            
    def toggle_item_selection(self, item_id):
        idx = int(item_id)
        if idx in self.selected_item_indices:
            self.selected_item_indices.remove(idx)
            checked = "[ ]"
        else:
            self.selected_item_indices.add(idx)
            checked = "[✓]"
            
        values = list(self.tree.item(item_id, "values"))
        values[0] = checked
        self.tree.item(item_id, values=values)
        self.update_selection_summary()
        
    def select_all_visible(self):
        for item_id in self.tree.get_children():
            idx = int(item_id)
            self.selected_item_indices.add(idx)
            values = list(self.tree.item(item_id, "values"))
            values[0] = "[✓]"
            self.tree.item(item_id, values=values)
        self.update_selection_summary()
        
    def deselect_all(self):
        self.selected_item_indices.clear()
        for item_id in self.tree.get_children():
            values = list(self.tree.item(item_id, "values"))
            values[0] = "[ ]"
            self.tree.item(item_id, values=values)
        self.update_selection_summary()
        
    def toggle_all_selection(self):
        if len(self.selected_item_indices) == len(self.discovered_items):
            self.deselect_all()
        else:
            self.select_all_visible()
            
    def update_selection_summary(self):
        count = 0
        total_size = 0
        for idx in self.selected_item_indices:
            if idx < len(self.discovered_items):
                item = self.discovered_items[idx]
                if not item['is_dir']:
                    count += 1
                    total_size += item['data_len']
        self.lbl_selected_summary.config(text=f"{count} files selected ({format_size(total_size)})")
        
    def filter_tree_display(self, *args):
        query = self.search_var.get().lower().strip()
        cat_filter = self.category_var.get()
        
        self.tree.delete(*self.tree.get_children())
        
        for idx, item in enumerate(self.discovered_items):
            # Category filter
            if cat_filter != "All" and item['category'] != cat_filter:
                continue
            # Search query
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
        self.tree.delete(*self.tree.get_children())
        self.update_selection_summary()
        
        self.is_scanning = True
        self.stop_requested = False
        self.btn_scan.config(state=tk.DISABLED, bg="#333348")
        self.btn_stop.config(state=tk.NORMAL, bg=self.c_danger, fg="#ffffff")
        self.btn_recover.config(state=tk.DISABLED)
        
        threading.Thread(target=self.scan_worker, args=(drive_path,), daemon=True).start()
        
    def stop_action(self):
        self.stop_requested = True
        self.lbl_footer_status.config(text="Stopping gracefully...")
        
    def scan_worker(self, drive_path):
        start_time = time.time()
        start_offset = 11 * 1024 * 1024 * 1024  # 11 GB
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
        seen_keys = set()
        
        try:
            while current < end_offset and not self.stop_requested:
                to_read = min(chunk_size, end_offset - current)
                buf = drive.read(to_read)
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
                                self.msg_queue.put(("item_found", entry))
                    idx += 1
                    
                current += len(buf)
                progress = (current - start_offset) / (end_offset - start_offset) * 100
                speed = ((current - start_offset) / (1024**2)) / max(time.time() - start_time, 0.001)
                self.msg_queue.put(("scan_progress", (progress, current, speed)))
                
        except Exception as e:
            self.msg_queue.put(("error", f"Scan error: {e}"))
        finally:
            drive.close()
            self.msg_queue.put(("scan_done", True))
            
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
                    
                self.msg_queue.put(("recovery_progress", (i, total, clean_name)))
                
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
                except Exception as err:
                    pass
        finally:
            drive.close()
            self.msg_queue.put(("recovery_done", recovered_count))
            
    # ==========================================================================
    # Queue Dispatcher
    # ==========================================================================
    
    def process_queue(self):
        try:
            while True:
                msg_type, data = self.msg_queue.get_nowait()
                
                if msg_type == "item_found":
                    idx = len(self.discovered_items)
                    self.discovered_items.append(data)
                    # Automatically select high value files (Godot, Code, 3D) by default
                    if data['category'] in ['Godot / Game', 'Code & Dev', '3D & Assets']:
                        self.selected_item_indices.add(idx)
                    # Insert into tree if matches current filter
                    cat_filter = self.category_var.get()
                    query = self.search_var.get().lower().strip()
                    if (cat_filter == "All" or data['category'] == cat_filter) and (not query or query in data['name'].lower()):
                        checked = "[✓]" if idx in self.selected_item_indices else "[ ]"
                        type_str = "Folder" if data['is_dir'] else "File"
                        size_str = "-" if data['is_dir'] else format_size(data['data_len'])
                        self.tree.insert("", tk.END, iid=str(idx), values=(
                            checked, data['name'], data['category'], size_str, f"Cluster {data['first_cluster']}", type_str
                        ))
                    self.update_selection_summary()
                    
                elif msg_type == "scan_progress":
                    pct, curr_bytes, speed = data
                    self.progress_bar['value'] = pct
                    gb_done = curr_bytes / (1024**3)
                    self.lbl_footer_status.config(text=f"Scanning: {gb_done:.1f} GB ({pct:.1f}%) | Speed: {speed:.1f} MB/s | Found {len(self.discovered_items)} items")
                    
                elif msg_type == "scan_done":
                    self.is_scanning = False
                    self.btn_scan.config(state=tk.NORMAL, bg=self.c_accent)
                    self.btn_stop.config(state=tk.DISABLED, bg="#333348", fg=self.c_text_dim)
                    self.btn_recover.config(state=tk.NORMAL)
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
                    self.btn_stop.config(state=tk.DISABLED, bg="#333348", fg=self.c_text_dim)
                    self.btn_recover.config(state=tk.NORMAL)
                    self.progress_bar['value'] = 100
                    self.lbl_footer_status.config(text=f"Recovery finished! Saved {data} files to destination.")
                    messagebox.showinfo("Success", f"Recovery complete! Successfully recovered {data} files to:\n{self.output_dir_var.get()}")
                    
                elif msg_type == "error":
                    messagebox.showerror("Notice", data)
                    
        except queue.Empty:
            pass
            
        self.root.after(100, self.process_queue)

def main():
    root = tk.Tk()
    app = ExFatRescueApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()
