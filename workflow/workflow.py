from stage import Stage, Status
from perf_model import StagePerfModel, config_pairs, step_names
import json
import os
import numpy as np
from utils import MyThread, MyProcess, extract_info_from_log, clear_data
import time

class Workflow:
    def __init__(self, config_file, boto3_client_ = None) -> None:
        assert isinstance(config_file, str)
        
        self.workflow_name = None
        self.boto3_client = boto3_client_
        
        self.stages = []
        self.sources = []
        self.sinks = []
        
        config = json.load(open(config_file, 'r'))
        self.parse_config(config)
    
    def parse_config(self, config) -> None:
        num = config['num_stages']
        self.workflow_name = config['workflow_name']
        for i in range(num):
            stage = Stage(self.workflow_name, config[str(i)]['stage_name'], i)
            self.stages.append(stage)
            
        for index, stage in enumerate(self.stages):
            if 'input_files' in config[str(index)]:
                stage.input_files = config[str(index)]['input_files']
            if 'output_files' in config[str(index)]:
                stage.output_files = config[str(index)]['output_files']
            if 'read_pattern' in config[str(index)]:
                stage.read_pattern = config[str(index)]['read_pattern']
            if 'allow_parallel' in config[str(index)]:
                if config[str(index)]['allow_parallel'] == 'false' or\
                    config[str(index)]['allow_parallel'] == 'False':
                        stage.allow_parallel = False
                        stage.perf_model.update_allow_parallel(False)
                        
            if 'extra_args' in config[str(index)]:
                stage.extra_args = config[str(index)]['extra_args']
                        
            parents = config[str(index)]['parents']
            for p in parents:
                stage.add_parent(self.stages[p])
            children = config[str(index)]['children']
            for c in children:
                stage.add_child(self.stages[c])
                
        # check dependency
        for stage in self.stages:
            for p in stage.parents:
                assert stage in p.children
                
            for c in stage.children:
                assert stage in c.parents
        
        # select sources and sinks
        for stage in self.stages:
            if len(stage.parents) == 0:
                self.sources.append(stage)

            if len(stage.children) == 0:
                self.sinks.append(stage)
                
        for stage in self.sources:
            stage.status = Status.READY
        
        # check Directed Acyclic Graph
        assert self.check_dag()
        
        
    def check_dag(self):
        queue = self.sources.copy()
        in_degrees = [len(s.parents) for s in self.stages]
        
        count = 0
        while len(queue) > 0:
            node = queue.pop(0)
            count += 1
            
            for child in node.children:
                ids = child.stage_id
                in_degrees[ids] -= 1
                
                if in_degrees[ids] == 0:
                    queue.append(child)
                    
        return count >= len(self.stages)
    
    def check_finished(self, threads):
        assert isinstance(threads, list)
        
        for ids, thread in enumerate(threads):
            if self.stages[ids].status == Status.RUNNING:
                if thread is not None and not thread.is_alive():
                    # print('Stage', ids, 'finished')
                    self.stages[ids].status = Status.FINISHED
        
        for stage in self.stages:
            if stage.status != Status.FINISHED:
                return False
        return True
    
    def update_stage_status(self):
        for stage in self.stages:
            if stage.status == Status.WAITING:
                is_ready = True
                for p in stage.parents:
                    if p.status != Status.FINISHED:
                        is_ready = False
                        break
                if is_ready:
                    stage.status = Status.READY
                    
        # for s in self.stages:
        #     print(s.stage_id, ':' , s.status, end=' ')
        # print()
    
    def init_stage_status(self):
        for stage in self.stages:
            stage.status = Status.WAITING
    
    def lazy_execute(self):
        # Stage info is only changed in main thread
        threads = [None for i in range(len(self.stages))]
        
        while not self.check_finished(threads):
            stage = None
            for s in self.stages:
                if s.status == Status.READY:
                    stage = s
                    break
            if stage is None:
                self.update_stage_status()
                continue
            # is_running = False
            # for s in self.stages:
            #     if s.status == Status.RUNNING:
            #         is_running = True
            #         break
            # if is_running:
            #     continue
            stage.status = Status.RUNNING
            thread = MyThread(target=stage.execute, args=None)
            threads[stage.stage_id] = thread
            thread.start()
            
            self.update_stage_status()
            
        for thread in threads:
            assert not thread.is_alive()
            
        for thread in threads:
            thread.join()
            
        res_list = []
        for thread in threads:
            res_list.append(thread.result)
            
        return res_list
    
    def timeline_execute(self):
        raise NotImplementedError
    
    def eager_execute(self):
        raise NotImplementedError

    def profile(self, num_epochs=2) -> str:
        # Use different configurations to profile, 
        # profile multiple epochs under the same configuration
        # and write the results to a storage (S3 or local) or pass to the performance model
        assert isinstance(num_epochs, int) and num_epochs > 0 
        
        # Organize the results into an array divided according to each stage
        # res is a dict of stage_name, res[stage_name] is a dict of step_name;
        # res[stage_name][step_name] is a 3D array with shape (num_epochs, num_config_pairs, 2)
        res = dict()
        for stage in self.stages:
            res[stage.stage_name] = dict()
            for step_name in step_names:
                res[stage.stage_name][step_name] = np.zeros((num_epochs, len(config_pairs), 2)).tolist()
        
        try:
            for config_pair in config_pairs:
                print('Config:', config_pair)
                for stage in self.stages:
                    mem_size, num_func = config_pair
                    if not stage.update_config(mem_size, num_func):
                        raise Exception('Config update failed')
                
                for epoch_id in range(num_epochs):
                    print('Epoch:', epoch_id)
                    self.init_stage_status()
                    clear_dir = self.workflow_name + '/stage'
                    clear_dir = clear_dir.replace('-', '_')  # adequate for ML-Pipeline and ML_Pipeline
                    clear_data(clear_dir)
                    epoch_res = self.lazy_execute()

                    infos = []
                    time_list = []
                    times_list = []
                    for ids, r in enumerate(epoch_res):
                        l = []
                        for ids_, result in enumerate(r):
                            if ids_ == 0:
                                time_list.append(result)
                                continue
                            info = extract_info_from_log(result[1])
                            infos.append(info)
                            rd = json.loads(result[0])
                            if 'statusCode' not in rd:
                                print(rd)
                                raise Exception('Lambda execution error')
                            rd = json.loads(rd['body'])
                            l.append(rd['breakdown'])
                        times_list.append(l)
                    cost = 0
                    for info in infos:
                        cost += info['bill']
                    print('Cost:', cost, '$')
                    for idx, t in enumerate(time_list):
                        print('Stage', idx, 'time:', t)
                        print(times_list[idx])
                        tt = np.array(times_list[idx])
                        tt = tt.T[:4]
                        tt[1] = tt[3] - tt[0] - tt[2]  # Add potential multi-thread overhead to compute
                        avg_tt = np.mean(tt, axis=1)
                        max_tt = np.percentile(tt, 95, axis=1)
                        cold_tt_avg = t - avg_tt[3]
                        cold_tt_max = t - np.sum(max_tt[:3])
                        print('Avg:', avg_tt)
                        print('Max:', max_tt)
                        print('Cold:', cold_tt_avg, cold_tt_max)
                        print('\n')
                        stage_name = self.stages[idx].stage_name
                        config_id = config_pairs.index(config_pair)
                        res[stage_name]['cold'][epoch_id][config_id] = [cold_tt_avg, cold_tt_max]
                        res[stage_name]['read'][epoch_id][config_id] = [avg_tt[0], max_tt[0]]
                        res[stage_name]['compute'][epoch_id][config_id] = [avg_tt[1], max_tt[1]]
                        res[stage_name]['write'][epoch_id][config_id] = [avg_tt[2], max_tt[2]]
                    print('\n\n')
                print('\n\n\n')

            # Persist the results
            prof_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            prof_dir = os.path.join(prof_dir, 'profiles/')
            if not os.path.exists(prof_dir):
                os.mkdir(prof_dir)
            prof_path = self.workflow_name + '_profile.json'
            prof_path = prof_path.replace('/', '-')  # transfer '/' in profile_path to '-'
            prof_path = os.path.join(prof_dir, prof_path)
            json.dump(res, open(prof_path, 'w'))
            return prof_path
        
        except Exception as e:
            print(res)
            print('\n\n')
            raise e

    def train_perf_model(self, profile_path):
        t0 = time.time()
        assert isinstance(profile_path, str) and os.path.exists(profile_path)
        for stage in self.stages:
            # if (self.stages.index(stage) != 1):
            #     continue
            stage.perf_model.train(profile_path)
        t1 = time.time()
        print('Training time:', t1 - t0, 's')

    def find_paths(self):
        paths = []
        # Initialize the queue with the sources, each source is a path on its own
        queue = [[source] for source in self.sources]

        while len(queue) > 0:
            # Take the first path from the queue
            path = queue.pop(0)
            # Get the last node from the path
            node = path[-1]
            # If this node is a sink, we found a path from source to sink
            if node in self.sinks:
                paths.append(path)
            else:
                # Otherwise, extend the path with the node's children and put it back in the queue
                for child in node.children:
                    if child not in path:  # Avoid cycles
                        new_path = list(path)
                        new_path.append(child)
                        queue.append(new_path)
        return paths
    
    def print_paths(self, paths):
        assert isinstance(paths, list)
        for path in paths:
            print('Path:', end=' ')
            for stage in path:
                if path.index(stage) != len(path) - 1:
                    print(stage.stage_name, end='-->')
                else:
                    print(stage.stage_name)      

    def predict(self, mode='latency', input_size=None):
        assert isinstance(input_size, int) and input_size > 0
        assert mode in ['latency', 'cost']
        if mode == 'latency':
            paths = self.find_paths()
            latency = 0.0
            for path in paths:
                tmp_latency = 0.0
                parent_d = 1
                for stage in path:
                    tmp_latency += stage.perf_model.predict(stage.config['memory'], 
                                                            stage.num_func, mode, 
                                                            parent_d=parent_d,
                                                            input_size=input_size)
                    parent_d = stage.num_func
                if paths.index(path) == 0:
                    latency = tmp_latency
                elif tmp_latency < latency:
                    latency = tmp_latency
        else:
            cost = 0.0
            for stage in self.stages:
                cost += stage.perf_model.predict(stage.config['memory'],
                                                 stage.num_func, mode,
                                                 input_size=input_size)
            return cost

    def sample_offline(self, num_samples):
        assert isinstance(num_samples, int) and num_samples > 0
        res = []
        for stage in self.stages:
            res += stage.perf_model.sample_offline(num_samples)
        sample_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sample_dir = os.path.join(sample_dir, 'samples/')
        if not os.path.exists(sample_dir):
            os.mkdir(sample_dir)
        sample_path = self.workflow_name + '_samples.json'
        sample_path = sample_path.replace('/', '-')  # transfer '/' in profile_path to '-'
        sample_path = os.path.join(sample_dir, sample_path)
        json.dump(res, open(sample_path, 'w'))
        return sample_path

    def fuse_samples_online(self, sample_path, num_fused_samples):
        assert isinstance(sample_path, str) and sample_path.endswith('.json')
        assert isinstance(num_fused_samples, int) and num_fused_samples > 0
        # TODO: fuse like k-means
        pass

    def close_pools(self):
        for stage in self.stages:
            stage.close_pool()
    
    def __getstate__(self):
        self_dict = self.__dict__.copy()
        del self_dict['pool']
        return self_dict
    
    def __del__(self):
        pass


if __name__ == '__main__':
    test_mode = 'perf_model' # 'step_by_step' 'lazy' 'perf_model'
    
    if test_mode == 'step_by_step':
        wf = Workflow( './tpcds-dsq95.json')
        stage = wf.stages[0]
        stage.num_func = 16
        
        stage = wf.stages[1]
        stage.num_func = 1
        
        stage = wf.stages[2]
        stage.num_func = 16
        
        stage = wf.stages[3]
        stage.num_func = 16
        
        stage = wf.stages[4]
        stage.num_func = 16
        
        stage = wf.stages[5]
        stage.num_func = 16
        
        stage = wf.stages[6]
        stage.num_func = 16
        
        stage = wf.stages[0]
        print(wf.workflow_name, stage)
        stage.status = Status.RUNNING
        stage.num_func = 64
        
        t1 = time.time()
        thread = MyThread(target=stage.execute, args=None)
        thread.start()
        thread.join()
        res = thread.result
        # res = stage.execute()
        t2 = time.time()
        print('Number of functions:', stage.num_func)
        print(t2 - t1)
        for idx, result in enumerate(res):
            if idx == 0:
                continue
            rd = json.loads(result[0])
            print(rd)
            if 'statusCode' not in rd:
                print(rd)
            rd = json.loads(rd['body'])
            print(rd['breakdown'])
            
        print('\n\n')
        wf.close_pools()
    elif test_mode == 'lazy':
        wf = Workflow( './ML-pipeline.json')
        wf.stages[0].num_func = 1
        wf.stages[1].num_func = 3
        wf.stages[2].num_func = 3
        wf.stages[3].num_func = 1
        # wf = Workflow( './tpcds-dsq95.json')
        # wf.stages[0].num_func = 20
        # wf.stages[1].num_func = 1
        # wf.stages[2].num_func = 20
        # wf.stages[3].num_func = 20
        # wf.stages[4].num_func = 20
        # wf.stages[5].num_func = 20
        # wf.stages[6].num_func = 20
        # wf.stages[7].num_func = 1
        for stage in wf.stages:
            print(str(stage.stage_id) + ':' + str(stage.num_func), end=' ')
        print()
        t1 = time.time()
        res = wf.lazy_execute()
        t2 = time.time()
        print('Time:', t2 - t1)
        print(res)
        infos = []
        time_list = []
        times_list = []
        for ids, r in enumerate(res):
            l = []
            for ids_, result in enumerate(r):
                if ids_ == 0:
                    time_list.append(result)
                    continue
                info = extract_info_from_log(result[1])
                infos.append(info)
                
                rd = json.loads(result[0])
                if 'statusCode' not in rd:
                    print(rd)
                rd = json.loads(rd['body'])
                l.append(rd['breakdown'])
            times_list.append(l)
        cost = 0
        for info in infos:
            cost += info['bill']
        print('Cost:', cost, '$')
        for idx, t in enumerate(time_list):
            print('Stage', idx, 'time:', t)
            print(times_list[idx])
        print('Idea DAG Execution Time:', time_list[0] + time_list[1]\
              + time_list[2] + time_list[3])
        print('\n\n')
        wf.close_pools()
    elif test_mode == 'perf_model':
        wf = Workflow( './ML-pipeline.json')
        # p = wf.profile()
        # print(p)
        p = '../profiles/ML-Pipeline_profile.json'
        wf.train_perf_model(p)
        # pr =wf.stages[0].perf_model.predict(1024, 1, 4)
        # wf.print_paths(wf.find_paths())
        wf.close_pools()
    else:
        raise NotImplementedError