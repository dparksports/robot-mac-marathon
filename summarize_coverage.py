import glob, os, datetime
from collections import defaultdict

def parse_time(filename):
    name = os.path.splitext(os.path.basename(filename))[0]
    parts = name.split('_')
    if len(parts) >= 3:
        try:
            d = datetime.datetime.strptime(f"{parts[1]}_{parts[2]}", "%Y-%m-%d_%H-%M-%S")
            return d
        except ValueError:
            pass
    return None

date_hours = defaultdict(set)
total_files = 0
for sdir in sorted(glob.glob('security_*')):
    for f in glob.glob(os.path.join(sdir, '*.mov')) + glob.glob(os.path.join(sdir, '*.m4a')):
        d = parse_time(f)
        if d:
            date_hours[d.date()].add(d.hour)
            total_files += 1

print("🕒 Recording Coverage: All Dates\n" + "="*35)

if not date_hours:
    print("No recordings found in this date range.")
else:
    for date in sorted(date_hours.keys()):
        hours = sorted(list(date_hours[date]))
        if hours:
            # Group contiguous hours
            ranges = []
            start = hours[0]
            prev = hours[0]
            for h in hours[1:]:
                if h == prev + 1:
                    prev = h
                else:
                    ranges.append(f"{start:02d}:00 - {prev:02d}:59")
                    start = h
                    prev = h
            ranges.append(f"{start:02d}:00 - {prev:02d}:59")
            print(f"📅 {date.strftime('%b %d')}: {', '.join(ranges)}")
            
print(f"\nTotal files in this range: {total_files}")
