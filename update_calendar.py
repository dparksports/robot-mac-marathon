import os
import glob
import re
from datetime import datetime

html_path = 'calendar.html'

def get_file_info(filepath):
    filename = os.path.basename(filepath)
    session = os.path.basename(os.path.dirname(filepath))
    size = os.path.getsize(filepath) if os.path.exists(filepath) else 0
    
    # parse timestamp from trigger_YYYY-MM-DD_HH-MM-SS.mov or audio_YYYY-MM-DD_HH-MM-SS.m4a
    name_no_ext = os.path.splitext(filename)[0]
    date_str, time_str = "", ""
    try:
        parts = name_no_ext.split('_')
        if len(parts) >= 3:
            date_str = parts[1]
            time_str = parts[2].replace('-', ':')[:5]  # HH:MM
    except:
        pass
        
    is_video = filepath.endswith('.mov')
    type_str = "video" if is_video else "audio"

    return f"  {{type:'{type_str}',name:'{filename}',date:'{date_str}',time:'{time_str}',size:{size},session:'{session}'}},"

files_data = []

# Gather all mov and m4a files
for sdir in sorted(glob.glob('security_*')):
    if not os.path.isdir(sdir): continue
    for f in sorted(glob.glob(os.path.join(sdir, '*.mov'))):
        files_data.append(get_file_info(f))
    for f in sorted(glob.glob(os.path.join(sdir, '*.m4a'))):
        files_data.append(get_file_info(f))

files_data_str = "\n".join(files_data)

with open(html_path, 'r') as f:
    content = f.read()

# Replace between const files = [ and ];
pattern = re.compile(r'const files = \[.*?\];', re.DOTALL)
replacement = f"const files = [\n{files_data_str}\n];"
new_content = pattern.sub(replacement, content)

with open(html_path, 'w') as f:
    f.write(new_content)

print("Updated calendar.html")
