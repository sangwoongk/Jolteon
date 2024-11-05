import re
import datetime
import os
import csv
import numpy as np

def extract_info_from_log(log):
    assert isinstance(log, str)
    
    billed_duration = 0
    duration = 0
    memory_size = 0
    memory_used = 0
    
    billed_duration = re.search(r"Billed Duration: (\d+)", log).group(1)
    duration = re.search(r"Duration: (\d+.\d+)", log).group(1)
    memory_used = re.search(r"Max Memory Used: (\d+)", log).group(1)
    memory_size = re.search(r"Memory Size: (\d+)", log).group(1)
    
    info = {
        "billed_duration": float(billed_duration),
        "duration": float(duration),
        "memory_size": float(memory_size),
        "memory_used": float(memory_used)
    }
    
    bill = caculate_bill(info)
    info['bill'] = bill
    
    return info
    
def caculate_bill(info):
    assert isinstance(info, dict)
    bill = info['billed_duration'] * info['memory_size'] / 1024 * 0.0000000167 + 0.2 / 1000000
    return bill

# <<< swkim
def orca_extract_info_from_log(lambda_ret, lambda_log):
    assert isinstance(lambda_ret, dict)
    assert isinstance(lambda_log, str)
    
    billed_duration = 0
    duration = 0
    memory_size = 0
    memory_used = 0
    
    billed_duration = re.search(r"Billed Duration: (\d+)", lambda_log).group(1)
    duration = re.search(r"Duration: (\d+.\d+)", lambda_log).group(1)
    memory_used = re.search(r"Max Memory Used: (\d+)", lambda_log).group(1)
    memory_size = re.search(r"Memory Size: (\d+)", lambda_log).group(1)

    up_type = lambda_ret['orca']['up_type']
    down_type = lambda_ret['orca']['down_type']
    up_time = lambda_ret['upload']
    down_time = lambda_ret['download']
    
    info = {
        "billed_duration": float(billed_duration),
        "duration": float(duration),
        "memory_size": float(memory_size),
        "memory_used": float(memory_used),
        "up_type": up_type,
        "down_type": down_type,
        "up_time": float(up_time),
        "down_time": float(down_time)
    }
    
    bill = orca_calculate_bill(info)
    info['bill'] = bill
    
    return info

def orca_calculate_bill(info):
    assert isinstance(info, dict)
    lambda_1gb_per_big = 0.00166667	# 100s (1000 * 100 ms)
    s3_up_cost_big = 0.5
    s3_down_cost_big = 0.04
    redis_up_cost_big = 0.5666667
    redis_down_cost_big = 0.5666667

    up_used = 0 if info['up_time'] == 0 else 1
    down_used = 0 if info['down_time'] == 0 else 1

    bill = info['billed_duration'] * (info['memory_size'] / 1024) * lambda_1gb_per_big

    if info['up_type'] == 's3':
        bill += s3_up_cost_big * up_used
    elif info['up_type'] == 'redis':
        bill += redis_up_cost_big * up_used

    if info['down_type'] == 's3':
        bill += s3_down_cost_big * down_used
    elif info['down_type'] == 'redis':
        bill += redis_down_cost_big * down_used

    return bill

def orca_save_result(save_dir: str, file_prefix: str, num_vcpus: list, x_bound: list, result: list):
    now = datetime.datetime.now()
    now_str = now.strftime('%Y-%m-%d_%H_%M_%S')
    file_name = f'{file_prefix}_{now_str}.csv'

    try:
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)
    except OSError:
        print(f'Error: Failed to create {save_dir}')

    with open(os.path.join(save_dir, file_name), 'w') as f:
        writer = csv.writer(f, delimiter=',')
        first_row = ['num_vcpus']
        writer.writerow(first_row)
        writer.writerow(num_vcpus)
        writer.writerow([])

        bound_row = ['x_bound']
        writer.writerow(bound_row)
        for b in x_bound:
            writer.writerow([b])
        writer.writerow([])
        
        data_first_row = ['raw_data']
        data_second_row = list(result[0].keys())
        data_second_row.insert(0, 'name')

        writer.writerow(data_first_row)
        writer.writerow(data_second_row)
        for i, res in enumerate(result):
            row = list(res.values())
            row.insert(0, i)
            writer.writerow(row)

        writer.writerow([])
        writer.writerow(['statistics'])

        percentiles = [90, 95, 99]
        lat_row = ['latency']
        cost_row = ['cost']

        e2e_vals = [item['e2e'] for item in result]
        cost_vals = [item['cost'] for item in result]
        avg_e2e = np.average(e2e_vals)
        avg_cost = np.average(cost_vals)
        lat_row.append(avg_e2e)
        cost_row.append(avg_cost)

        for p in percentiles:
            lat_p = np.percentile(e2e_vals, p, interpolation='nearest')
            cost_p = np.percentile(cost_vals, p, interpolation='nearest')
            lat_row.append(lat_p)
            cost_row.append(cost_p)

        percentile_head = ['type', 'average']
        for p in percentiles:
            percentile_head.append(f'P{p}')

        writer.writerow(percentile_head)
        writer.writerow(lat_row)
        writer.writerow(cost_row)
# <<< swkim