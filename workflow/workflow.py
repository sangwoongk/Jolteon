from multiprocessing import Pool
import time
import json
import os
from deprecation import deprecated
import numpy as np

from stage import Stage, Status, PerfModel
from perf_model import StagePerfModel, config_pairs, step_names, get_config_pairs
from perf_model_dist import config_pairs as dist_config_pairs, get_config_pairs_dist
from utils import MyThread, MyProcess, PCPSolver, extract_info_from_log, clear_data, orca_extract_info_from_log

class Workflow:
    def __init__(self, config_file, perf_model_type = 0, boto3_client_ = None) -> None:
        assert isinstance(config_file, str)
        
        self.workflow_name = None
        self.boto3_client = boto3_client_
        
        self.stages = []
        self.sources = []
        self.sinks = []

        self.critical_path = None
        self.secondary_path = None
        
        self.perf_model_type = perf_model_type
        
        config = json.load(open(config_file, 'r'))
        self.parse_config(config)
    
    def parse_config(self, config) -> None:
        num = config['num_stages']
        self.workflow_name = config['workflow_name']
        self.is_orca = self.workflow_name in ['MLPipeline', 'ImageProcessing']  # swkim

        for i in range(num):
            func_name = None
            if self.is_orca:    # swkim
                func_name = config[str(i)]["stage_name"]
            stage = Stage(self.workflow_name, config[str(i)]['stage_name'], i, self.perf_model_type, func_name_=func_name)
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

            # <<< swkim
            if 'orca_input' in config[str(index)]:
                stage.extra_args = config[str(index)]['orca_input']
            # <<< swkim

            parents = config[str(index)]['parents']
            for p in parents:
                stage.add_parent(self.stages[p])
            children = config[str(index)]['children']
            for c in children:
                stage.add_child(self.stages[c])
        
        if self.perf_model_type == PerfModel.Jolteon.value:
            for stage in self.stages:
                has_parent = len(stage.parents) > 0
                stage.perf_model.update_has_parent(has_parent)
                
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

        # config critical path and secondary path
        if 'critical_path' in config:
            self.critical_path = [self.stages[i] for i in config['critical_path']]
        if 'secondary_path' in config:
            self.secondary_path = [self.stages[i] for i in config['secondary_path']]
                
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
            res_list.append(thread.result[0])
            
        return res_list
    
    def timeline_execute(self):
        raise NotImplementedError
    
    def eager_execute(self):
        raise NotImplementedError
    
    def profile(self, num_epochs = 3) -> str:
        # if self.perf_model_type == PerfModel.Jolteon.value:
        #     return self.profile_jolteon(num_epochs)
        # elif self.perf_model_type == PerfModel.Distribution.value:
        #     return self.profile_dist(num_epochs)
        # elif self.perf_model_type == PerfModel.Analytical.value:
        #     return self.profile_analytic(num_epochs)
        # else:
        #     raise ValueError('Invalid performance model type: %d' % self.perf_model_type)
        if self.perf_model_type in [PerfModel.Jolteon.value, PerfModel.Distribution.value, PerfModel.Analytical.value]:
            return self.profile_jolteon(num_epochs)
        else:
            raise ValueError('Invalid performance model type: %d' % self.perf_model_type)
    
    def profile_jolteon(self, num_epochs) -> str:
        # Use different configurations to profile, 
        # profile multiple epochs under the same configuration
        # and write the results to a storage (S3 or local) or pass to the performance model
        assert isinstance(num_epochs, int) and num_epochs > 0 
        
        # Organize the results into an array divided according to each stage
        # res is a dict of stage_name, res[stage_name] is a dict of step_name;
        # res[stage_name][step_name] is a 3D array with shape (num_epochs, num_config_pairs, 2)
        res = dict()
        config_pairs_ = get_config_pairs(self.workflow_name)
        for stage in self.stages:
            res[stage.stage_name] = dict()
            for step_name in step_names:
                res[stage.stage_name][step_name] = np.zeros((num_epochs, len(config_pairs_), 2)).tolist()
        
        try:
            for config_pair in config_pairs_:
                print('Config:', config_pair)
                for stage in self.stages:
                    mem_size, num_func = config_pair
                    if not stage.update_config(mem_size, num_func):
                        raise Exception('Config update failed')
                    '''
                        Warm-up dummy run is currently not adequate, since too frequent 
                    Lambda invocation cause the following error:
                        botocore.errorfactory.ResourceConflictException: An error occurred 
                        (ResourceConflictException) when calling the UpdateFunctionConfiguration 
                        operation: The operation cannot be performed at this time. 
                        An update is in progress for resource: arn:aws:lambda:us-east-1:325476609965:function:ML-Pipeline-stage3
                    
                        This is probably because the asynchronous update of Lambda configuration or 
                    the collision of Lambda invocation and configuration update.
                    '''
                    # stage.status = Status.RUNNING
                    # r = stage.execute(dummy=1)
                
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
                            # <<< swkim
                            rd = json.loads(result[0])

                            if self.is_orca:
                                info = orca_extract_info_from_log(rd, result[1])
                                infos.append(info)
                                if 'data' not in rd:
                                    print(rd)
                                    raise Exception('Lambda execution error')

                                l.append(rd['jolteon_res'])
                            else:
                                info = extract_info_from_log(result[1])
                                infos.append(info)
                                if 'statusCode' not in rd:
                                    print(rd)
                                    raise Exception('Lambda execution error')

                                rd = json.loads(rd['body'])
                                l.append(rd['breakdown'])
                            # <<< swkim

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
                        config_id = config_pairs_.index(config_pair)
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
        
    @deprecated
    def profile_dist(self, num_epochs) -> str:
        assert isinstance(num_epochs, int) and num_epochs > 0
        res = dict()
        for stage in self.stages:
            res[stage.stage_name] = dict()
            res[stage.stage_name]['e2e'] = np.zeros((num_epochs, len(dist_config_pairs))).tolist()
            
        try:
            for config_pair in dist_config_pairs:
                print('Config:', config_pair)
                for stage in self.stages:
                    mem_size, num_func = config_pair
                    if not stage.update_config(mem_size, num_func):
                        raise Exception('Config update failed')
                    print('Updated {} config'.format(stage.stage_name))

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
                        stage_name = self.stages[idx].stage_name
                        config_id = dist_config_pairs.index(config_pair)
                        res[stage_name]['e2e'][epoch_id][config_id] = t
                    print('\n\n')
                print('\n\n\n')

            # Persist the results
            prof_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            prof_dir = os.path.join(prof_dir, 'profiles/')
            if not os.path.exists(prof_dir):
                os.mkdir(prof_dir)
            prof_path = self.workflow_name + '_profile_dist.json'
            prof_path = prof_path.replace('/', '-')  # transfer '/' in profile_path to '-'
            prof_path = os.path.join(prof_dir, prof_path)
            json.dump(res, open(prof_path, 'w'))
            return prof_path
            
        except Exception as e:
            print(res)
            print('\n\n')
            raise e

    @deprecated
    def profile_analytic(self, num_epochs) -> str:
        assert isinstance(num_epochs, int) and num_epochs > 0 
        
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
            prof_path = self.workflow_name + '_profile_analytic.json'
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
        get_config_pairs(self.workflow_name)
        get_config_pairs_dist(self.workflow_name)
        for stage in self.stages:
            stage.perf_model.train(profile_path)
            
        if self.perf_model_type == PerfModel.Distribution.value:
            # if len(self.sinks) != 1:
            #     raise Exception('Only support sink number == 1')
            
            for stage in self.stages:
                for p in stage.parents:
                    stage.perf_model.add_up_model(p.perf_model)
        
        t1 = time.time()
        print('Training time:', t1 - t0, 's\n')

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

    def predict(self, mode='latency'):
        assert mode in ['latency', 'cost']
        if mode == 'latency':
            paths = self.find_paths()
            latency = 0.0
            for path in paths:
                tmp_latency = 0.0
                parent_d = 0
                for stage in path:
                    tmp_latency += stage.perf_model.predict(stage.config['memory']/1792, 
                                                            stage.num_func, mode, 
                                                            parent_d=parent_d,
                                                            cold_percent=60)
                    parent_d = stage.num_func
                if paths.index(path) == 0:
                    latency = tmp_latency
                elif tmp_latency > latency:
                    latency = tmp_latency
            return latency
        else:
            cost = 0.0
            for stage in self.stages:
                parent_d = 0
                if not stage.allow_parallel:
                    for i in range(len(stage.parents)-1, -1, -1):
                        if stage.parents[i].allow_parallel:
                            parent_d = stage.parents[i].num_func
                            break
                cost += stage.perf_model.predict(stage.config['memory']/1792,
                                                 stage.num_func, mode,
                                                 parent_d=parent_d,
                                                 cold_percent=0)
            return cost

    def store_params(self):
        res = np.concatenate([stage.perf_model.params() for stage in self.stages])
        res = res.tolist()
        param_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        param_dir = os.path.join(param_dir, 'params/')
        if not os.path.exists(param_dir):
            os.mkdir(param_dir)
        param_path = self.workflow_name + '_params.json'
        param_path = param_path.replace('/', '-')  # transfer '/' in profile_path to '-'
        param_path = os.path.join(param_dir, param_path)
        json.dump(res, open(param_path, 'w'))
        return param_path

    def get_params(self):
        # calibration
        if self.workflow_name == 'ML-Pipeline':
            cold_percent = 60
        elif self.workflow_name == 'Video-Analytics':
            cold_percent = 85
        elif self.workflow_name == 'tpcds/dsq95':
            cold_percent = 75
        # <<< swkim
        elif self.workflow_name == 'MLPipeline':
            cold_percent = 60
        # <<< swkim
        res = np.concatenate([stage.perf_model.params(cold_percent) for stage in self.stages])
        res = res.tolist()
        return res

    def sample_offline(self, num_samples):
        assert isinstance(num_samples, int) and num_samples > 0
        res = np.concatenate([stage.perf_model.sample_offline(num_samples) for stage in self.stages], axis=1)
        res = res.tolist()
        sample_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sample_dir = os.path.join(sample_dir, 'samples/')
        if not os.path.exists(sample_dir):
            os.mkdir(sample_dir)
        sample_path = self.workflow_name + '_samples.json'
        sample_path = sample_path.replace('/', '-')  # transfer '/' in profile_path to '-'
        sample_path = os.path.join(sample_dir, sample_path)
        json.dump(res, open(sample_path, 'w'))
        return sample_path

    def sample_online(self, num_samples):
        assert isinstance(num_samples, int) and num_samples > 0
        res = np.concatenate([stage.perf_model.sample_offline(num_samples) for stage in self.stages], axis=1)
        res = res.tolist()
        return res

    def prune_samples(self, samples):
        # is_greater_than_others = np.ones(samples.shape[0], dtype=bool)
        is_less_than_another = np.zeros(samples.shape[0], dtype=bool)
        for i in range(samples.shape[0]):
            # is_greater_than_others[i] = np.all(np.all(samples[i] > samples[np.arange(res.shape[0]) != i], axis=1))
            is_less_than_another[i] = np.any(np.all(samples[i] < samples[np.arange(res.shape[0]) != i], axis=1))
        # res = samples[is_greater_than_others]
        res = samples[is_less_than_another]
        # print(res.shape[0])
        return res
    
    def update_workflow_config(self, mem_list, parall_list, real=True):
        assert isinstance(parall_list, list) and isinstance(mem_list, list)
        assert len(parall_list) == len(mem_list)
        assert len(parall_list) == len(self.stages)

        ret = []
        
        if real:
            for i in range(len(self.stages)):
                r = self.stages[i].update_config(mem_list[i], parall_list[i])
                ret.append(r)
        else:
            for i in range(len(self.stages)):
                self.stages[i].config['memory'] = mem_list[i]
                self.stages[i].num_func = parall_list[i]
        
        check = True
        for r in ret:
            if not r:
                check = False
                break
        return check

    def load_params(self, file_path):
        assert isinstance(file_path, str) and file_path.endswith('.json')
        return json.load(open(file_path, 'r'))
    
    def load_samples(self, file_path, num_samples):
        assert isinstance(file_path, str) and file_path.endswith('.json') and \
            isinstance(num_samples, int) and num_samples > 0
        
        samples = json.load(open(file_path, 'r'))
        return samples[:num_samples]

    def metadata_path(self, meta_type):
        assert meta_type in ['profiles', 'params', 'samples']
        meta_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        meta_dir = os.path.join(meta_dir, meta_type + '/')
        if meta_type == 'profiles':
            meta_type = 'profile'
        meta_path = self.workflow_name + '_' + meta_type + '.json'
        meta_path = meta_path.replace('/', '-')
        meta_path = os.path.join(meta_dir, meta_path)
        # if not os.path.exists(meta_path):
        #     raise Exception('Path does not exist: ' + meta_path)
        return meta_path
    
    '''
        Generate the python code for the objective function and constraints used by the solver
    Currently, we use the scipy.optimize as the solver and use numpy as the matrix library. 
    We now need users to provide the critical path and the (optional) secondary critical path 
    in order to avoid the occurrence of the np.max() function, which may cause undefined behavior.
    '''
    def generate_func_code(self, file_name, critical_path, secondary_path=None, 
                           cons_mode='latency', solver_type='scipy'):
        assert isinstance(file_name, str) and file_name.endswith('.py')
        assert isinstance(critical_path, list) 
        assert secondary_path is None or isinstance(secondary_path, list)
        assert cons_mode in ['latency', 'cost']
        assert solver_type == 'scipy'
        code_dir = os.path.dirname(os.path.abspath(__file__))
        code_path = os.path.join(code_dir, file_name)
        obj_mode = 'cost' if cons_mode == 'latency' else 'latency'

        parent_ids = [-1 for _ in range(len(self.stages))]
        for stage in self.stages:
            if not stage.allow_parallel:
                for i in range(len(stage.parents)-1, -1, -1):
                    if stage.parents[i].allow_parallel:
                        parent_ids[stage.stage_id] = stage.parents[i].stage_id
                        break

        s = 'import numpy as np\n\n'
        if solver_type == 'pyomo':
            s += 'import pyomo.environ as pyo\n'
            s += 'from pyomo.environ import *\n\n'

        # Generate objective function
        var = 'x'
        param = 'p'
        if solver_type == 'scipy':
            s += 'def objective_func(x, p):\n' + '    return '
        else:
            s += 'def objective_func(model):\n' + '    return '
            var = 'model.x'
            param = 'model.p'
        
        if obj_mode == 'latency':
            for stage in critical_path:
                s += stage.perf_model.generate_func_code(obj_mode, var, param, 
                                                         parent_ids[stage.stage_id], solver_type) + ' + '
        else:
            for stage in self.stages:
                s += stage.perf_model.generate_func_code(obj_mode, var, param, 
                                                         parent_ids[stage.stage_id], solver_type) + ' + '
        s = s[:-3]
        s += '\n\n'

        # Generate constraints
        bound = ' - b'
        func2_def = 'def constraint_func_2(x, p, b):\n' + '    return '
        if solver_type == 'scipy':
            s += 'def constraint_func(x, p, b):\n' + '    return '
            # <<< swkim
            # if cons_mode == 'cost': # Jolteon: error
            #     func2_def = 'def constraint_func_2(x, p):\n' + '    return '
            # <<< swkim
        else:
            s += 'def constraint_func(model):\n' + '    return '
            bound = ' - model.b <= 0'
            func2_def = 'def constraint_func_2(model):\n' + '    return '

        if cons_mode == 'latency':
            for stage in critical_path:
                s += stage.perf_model.generate_func_code(cons_mode, var, param, 
                                                         parent_ids[stage.stage_id], solver_type) + ' + '
            s = s[:-3]
            s += bound + '\n\n'
            if secondary_path is not None:
                s += func2_def
                for stage in secondary_path:
                    s += stage.perf_model.generate_func_code(cons_mode, var, param, 
                                                             parent_ids[stage.stage_id], solver_type) + ' + '
                s = s[:-3]
                s += bound + '\n\n'
        else:
            for stage in self.stages:
                s += stage.perf_model.generate_func_code(cons_mode, var, param, 
                                                         parent_ids[stage.stage_id], solver_type) + ' + '
            s = s[:-3]
            s += bound + '\n\n'
            # The time of the secondary path should be less than or equal to the time of the critical path
            if secondary_path is not None:
                s += func2_def
                critical_set = set(critical_path)
                secondary_set = set(secondary_path)
                c_s = critical_set - secondary_set
                s_c = secondary_set - critical_set
                assert len(c_s) > 0 and len(s_c) > 0
                for stage in c_s:
                    s += stage.perf_model.generate_func_code('latency', var, param, 
                                                             parent_ids[stage.stage_id], solver_type) + ' + '
                s = s[:-3] + ' - ('
                for stage in s_c:
                    s += stage.perf_model.generate_func_code('latency', var, param, 
                                                             parent_ids[stage.stage_id], solver_type) + ' + '
                s = s[:-3] + ')'
                if solver_type == 'pyomo':
                    s += ' <= 0'
                s += '\n\n'

        with open(code_path, 'w') as f:
            f.write(s)

    def close_pools(self):
        for stage in self.stages:
            stage.close_pool()
    
    def __getstate__(self):
        self_dict = self.__dict__.copy()
        del self_dict['pool']
        return self_dict
    
    def __del__(self):
        pass
