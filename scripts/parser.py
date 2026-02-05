import sys

threshold: float = -32.2

def parse(line: str) -> (float, float):
    data = line.split(",")
    start_freq,  end_freq = int(data[2].strip()) // 1000000, int(data[3].strip()) // 1000000 # MHz
    pwrs = [float(pwr.strip()) for pwr in data[6:]]
    
    for i in range(len(pwrs)):
        if pwrs[i] > threshold and start_freq > 100 and (start_freq < 5000 or start_freq > 5400) and (start_freq < 2000 or start_freq > 2700):
            freq = start_freq + i
            return (freq, pwrs[i])

    return None
