from abc import ABC, abstractmethod
import boto3
import utils
import time
import json
import math
import argparse
import numpy as np

from workflow import Workflow
from perf_model_dist import eq_vcpu_alloc
from utils import MyQueue, PCPSolver, extract_info_from_log, clear_data

# scheduler is responsible for tuning the launch time,
# number of function invocation and resource configuration
# for each stage
class Scheduler(ABC):
    def __init__(self, workflow: Workflow):
        self.workflow = workflow

class Jolteon(Scheduler):
    def __init__(self, workflow: Workflow, storage_mode='s3', max_sample_size=10000):
        super().__init__(workflow)
        self.storage_mode = storage_mode
        self.num_funcs = []
        self.num_vcpus = []

        self.bound_type = None
        self.bound = None
        self.risk = 0.05  # e.g., 5% risk for 95% latency <= bound
        self.confidence_error = 0.001  # The confidence error for the bounded performance
        self.max_sample_size = max_sample_size

        self.solver = None
        self.obj_params = None
        self.cons_params = None

    def set_bound(self, bound_type, bound, service_level):
        # service_level is the probability that the latencty or cost is less than the bound
        assert bound_type in ['latency', 'cost'] and bound > 0 and \
            service_level > 0 and service_level < 1
        self.bound_type = bound_type
        self.bound = bound
        self.risk = 1 - service_level

    def set_confidence(self, confidence):
        # confidence is the probability that the bounded performance is guaranteed
        assert confidence > 0 and confidence < 1
        self.confidence_error = 1 - confidence

    def store_params_and_samples(self):
        param_path = self.workflow.store_params()
        sample_path = self.workflow.sample_offline(self.max_sample_size)
        return param_path, sample_path
    
    def get_params_and_samples(self):
        self.obj_params = self.workflow.get_params()
        num_samples = PCPSolver.sample_size(len(self.workflow.stages), self.risk, 0, 
                                            self.confidence_error)
        self.cons_params = self.workflow.sample_online(num_samples)

    # Generate the objective and constraint functions code at './funcs.py'
    def generate_func_code(self, func_path='./funcs.py'):
        if func_path != './funcs.py' and func_path != 'funcs.py':
            raise ValueError('The function path must be ./funcs.py')
        self.workflow.generate_func_code(func_path, self.workflow.critical_path, 
                                         self.workflow.secondary_path, 
                                         cons_mode=self.bound_type)

    def search_config(self, param_path=None, sample_path=None, 
                      init_vals=None, x_bound=None, load=False):
        # Assume the functions have been generated
        from funcs import objective_func, constraint_func
        if self.workflow.secondary_path is not None:
            from funcs import constraint_func_2 

        if load:
            t0 = time.time()
            assert param_path is not None and sample_path is not None
            self.obj_params = self.workflow.load_params(param_path)
            num_samples = PCPSolver.sample_size(len(self.workflow.stages), self.risk, 0, 
                                                self.confidence_error)
            self.cons_params = self.workflow.load_samples(sample_path, num_samples)
            t1 = time.time()
            print('Load time:', t1-t0, 's\n\n')

        self.solver = PCPSolver(2*len(self.workflow.stages), objective_func, constraint_func, 
                                self.bound, self.obj_params, self.cons_params, 
                                risk=self.risk, confidence_error=self.confidence_error)

        res = self.solver.solve(init_vals=init_vals, x_bound=x_bound)

        print(res)

        if not res['status']:
            raise ValueError('Cannot find a feasible solution')
        x = res['x']
        for i, stage in enumerate(self.workflow.stages):
            num_funcs = int(x[2*i]) + 1
            if stage.allow_parallel is False:
                num_funcs = 1
            num_vcpus = x[2*i+1]
            if num_vcpus < 1:
                threshold = 1024 / 1792
                if num_vcpus > threshold:
                    num_vcpus = 1
                else:
                    num_vcpus = threshold
            else:
                num_vcpus = int(num_vcpus)
            self.num_funcs.append(int(num_funcs))
            self.num_vcpus.append(num_vcpus)
        
        print(self.num_funcs)
        print(self.num_vcpus)

        x = []
        for i in range(len(self.workflow.stages)):
            x.append(self.num_funcs[i])
            x.append(self.num_vcpus[i])
        print('Final obj:', objective_func(x, self.obj_params))

    def set_config(self):
        mem_list = [int(self.num_vcpus[i]*1792) for i in range(len(self.num_vcpus))]
        self.workflow.update_workflow_config(mem_list, self.num_funcs)


class Caerus(Scheduler):
    def __init__(self, workflow: Workflow, storage_mode = 's3'):
        super().__init__(workflow)
        self.storage_mode = storage_mode
        self.parallelism_ratio = []
        self.num_funcs = None
    
    # Get the test datasets
    def comp_ratio(self):
        file_size = []
        if self.storage_mode == 's3':
            for stage in self.workflow.stages:
                if stage.allow_parallel is False:
                    file_size.append(0)
                    continue
                inputs = stage.input_files
                cnt = 0
                for directory in inputs:
                    cnt += utils.get_dir_size(directory)
                file_size.append(cnt)
                
            sum_file_size = sum(file_size)
            self.parallelism_ratio = [item / sum_file_size for item in file_size]
        else:
            raise NotImplementedError()
        
    def set_config(self, total_parallelism):
        num_funcs = [int(item * total_parallelism) for item in self.parallelism_ratio]
        num_funcs = [item if item > 0 else 1 for item in num_funcs]
        for stage in self.workflow.stages:
            stage.num_func = num_funcs[stage.stage_id]
        self.num_funcs = num_funcs
            

class Orion(Caerus):
    def __init__(self, workflow: Workflow, storage_mode = 's3'):
        super().__init__(workflow)
        self.config = []
        self.max_memory = 1024 * 8
        self.memory_grain = 512
        
    def set_config(self, total_parallelism, latency, confidence):
        num_funcs = [int(item * total_parallelism) for item in self.parallelism_ratio]
        num_funcs = [item if item > 0 else 1 for item in num_funcs]
        for stage in self.workflow.stages:
            stage.num_func = num_funcs[stage.stage_id]
        self.num_funcs = num_funcs
        
        memory_config = self.bestfit(latency, confidence)
        
        self.workflow.update_workflow_config(memory_config, self.num_funcs)
        
    # Just BFS, but stop and return when one node is statisfied with the latency
    def bestfit(self, latency, confidence):
        # 128 is a configurable value
        memory_grain = self.memory_grain
        config_list = [memory_grain for i in range(len(self.workflow.stages))]

        search_spcae = MyQueue()
        search_spcae.push(config_list)

        while len(search_spcae) > 0:
            val = search_spcae.pop()

            for i in range(len(val)):
                new_val = val.copy()
                new_val[i] += memory_grain
                # Max limit
                if new_val[i] > self.max_memory:
                    continue
                search_spcae.push(new_val)
                
                dist = self.get_distribution(new_val)
                print(self.num_funcs, new_val, dist.probility(latency))
                print(dist)
                print('\n\n\n')
                if dist.probility(latency) >= confidence:
                    return new_val

        config_list = [self.max_memory for i in range(len(self.workflow.stages))]
        return config_list

    def get_distribution(self, config_list):
        vcpus = []
        for idx, memory in enumerate(config_list):
            vcpus.append(eq_vcpu_alloc(memory, self.num_funcs[idx]))
            
        for stage in self.workflow.stages:
            stage.perf_model.set_func_size(vcpus[stage.stage_id])
            
        dist = None
        for stage in self.workflow.sinks:
            tmp_dist = stage.perf_model.calculate()
            if dist is None:
                dist = tmp_dist
            else:
                dist.combine(tmp_dist, 1)
                
        return dist

class Ditto(Scheduler):
    def __init__(self, workflow: Workflow, storage_mode = 's3'):
        super().__init__(workflow)
        self.storage_mode = storage_mode
        self.parallelism_ratio = [None for i in range(len(self.workflow.stages))]
        self.num_funcs = None
        
        # Init the virtual DAG
        self.virtual_stages = []
        self.sources = []
        self.sinks = []
        for stage in self.workflow.stages:
            param_a, _ = stage.perf_model.get_params()
            param_a = abs(param_a)  # Fit for not allow_parallel stages, whose a is negative
            new_stage = self.Virtual_Stage(param_a, stage.stage_id)
            self.virtual_stages.append(new_stage)
            if stage in self.workflow.sources:
                self.sources.append(new_stage)
            if stage in self.workflow.sinks:
                self.sinks.append(new_stage)
                
        # Build the virtual DAG
        for stage in self.workflow.stages:
            idx = stage.stage_id
            for p in stage.parents:
                self.virtual_stages[idx].add_parent(self.virtual_stages[p.stage_id])
                
            for c in stage.children:
                self.virtual_stages[idx].add_child(self.virtual_stages[c.stage_id])
                
        # Tune the virtual DAG, delete the redundant edges
        for stage in self.virtual_stages:
            maintain_list = []
            for idx, c in enumerate(stage.children):
                if stage.max_distance(c) == 1:
                    maintain_list.append(idx)

            new_children = [stage.children[idx] for idx in maintain_list]
            for idx, c in enumerate(stage.children):
                if idx in maintain_list:
                    continue
                c.parents.remove(stage)
            stage.children = new_children
        
    class Virtual_Stage:
        def __init__(self, param_a, idx = None):
            self.param_a = param_a
            self.stage_id = idx
            self.parents = []
            self.children = []
            
            self.merge_type = None     # 0 is parent-child, 1 is two siblings
            self.merge_stages = None
            self.merge_ratio = None
            
        def add_parent(self, parent):
            assert isinstance(parent, Ditto.Virtual_Stage)
            self.parents.append(parent)
            
        def add_child(self, child):
            assert isinstance(child, Ditto.Virtual_Stage)
            self.children.append(child)
            
        def check_parent(self, parent):
            assert isinstance(parent, Ditto.Virtual_Stage)
            if len(self.parents) == 0:
                return False
            if parent in self.parents:
                return True
            for p in self.parents:
                if p.check_parent(parent):
                    return True
            return False
                
        def check_child(self, child):
            assert isinstance(child, Ditto.Virtual_Stage)
            if len(self.child) == 0:
                return False
            if child in self.children:
                return True
            for c in self.children:
                if c.check_child(child):
                    return True
            return False
        
        # Only support 'v_stage' is a child stage of self
        def max_distance(self, v_stage):
            assert isinstance(v_stage, Ditto.Virtual_Stage)
            if v_stage == self:
                return 0
            
            ret = -1
            for c in self.children:
                val = c.max_distance(v_stage)
                if val >= 0:
                    ret = max(ret, val + 1)
            return ret
        
        def reverse_deepest_stages(self):
            if len(self.parents) == 0:
                return [self]
            
            ret = []
            for c in self.parents:
                val = c.reverse_deepest_stages()
                ret.extend(val)
            
            return ret
        
        def __str__(self):
            return 'stage index: ' + str(self.stage_id)
                    
    
    @staticmethod   
    def merge(stage0, stage1, merge_type):
        assert isinstance(stage0, Ditto.Virtual_Stage)
        assert isinstance(stage1, Ditto.Virtual_Stage)
        
        if merge_type == 0:
            a0 = stage0.param_a
            a1 = stage1.param_a
            new_a = math.sqrt(a0) + math.sqrt(a1)
            new_a = new_a * new_a
            
            ratio = a0 / a1
            ratio = math.sqrt(ratio)
            
            new_stage = Ditto.Virtual_Stage(new_a)
            new_stage.merge_type = 0
            new_stage.merge_stages = [stage0, stage1]
            new_stage.merge_ratio = ratio
            
            return new_stage
        elif merge_type == 1:
            a0 = stage0.param_a
            a1 = stage1.param_a
            
            new_a = a0 + a1
            
            ratio = a0 / a1
            
            new_stage = Ditto.Virtual_Stage(new_a)
            new_stage.merge_type = 1
            new_stage.merge_stages = [stage0, stage1]
            new_stage.merge_ratio = ratio
            
            return new_stage
        else:
            raise ValueError('Invalid merge type')
    
    # stage0 is the parent stage, stage1 is the child stage
    @staticmethod
    def distance(stage0, stage1):
        if stage0 == stage1:
            return 0
        
        ret = -1
        for c in stage0.children:
            val = Ditto.distance(c, stage1)
            if val >= 0:
                ret = min(ret, val + 1) if ret >= 0 else val + 1
        
        return ret
    
    # Assign the degree of parallelism to each stage
    # 'v_stage' is the final stage of the virtual DAG   
    def assign(self, v_stage, degree):
        assert isinstance(v_stage, Ditto.Virtual_Stage)
        assert degree > 0
        
        if v_stage.stage_id is not None:
            self.parallelism_ratio[v_stage.stage_id] = degree
            return
        
        con_stages = v_stage.merge_stages
        stage0 = con_stages[0]
        stage1 = con_stages[1]
        
        assert isinstance(stage0, Ditto.Virtual_Stage)
        assert isinstance(stage1, Ditto.Virtual_Stage)
        
        ratio1 = 1 / (v_stage.merge_ratio + 1)
        ratio0 = 1 - ratio1
        
        degree0 = degree * ratio0
        degree1 = degree * ratio1
            
        self.assign(stage0, degree0)
        self.assign(stage1, degree1)
        
        
    # Get the ratio of the entire workflow
    def comp_ratio(self, obj = 'latency'):
        if obj == 'latency':
            if len(self.sinks) != 1:
                raise ValueError('Do not support the current virtual DAG in Ditto')
            
            sink_stage = self.sinks[0]
            final_stage = None
            
            stop = True
            while stop:
                idx = None
                dis_val = -1
                leaves = sink_stage.reverse_deepest_stages()
                for v_stage in leaves:
                    dis = Ditto.distance(v_stage, sink_stage)
                    if dis > dis_val:
                        dis_val = dis
                        idx = leaves.index(v_stage)
                assert dis_val >= 0
                cur_stage = leaves[idx]
                
                if cur_stage == sink_stage:
                    stop = False
                    final_stage = cur_stage
                    break
                
                assert len(cur_stage.children) <= 1
                if len(cur_stage.children) == 0:
                    final_stage = cur_stage
                    break
                
                c_stage = cur_stage.children[0]
                sib_stages = c_stage.parents
                merge_stage = None
                
                # Merge sibling stages
                for idx, s in enumerate(sib_stages):
                    if idx == 0:
                        merge_stage = s
                        continue
                    
                    merge_stage = Ditto.merge(merge_stage, s, 1)
                
                # Merge parent-child stage
                merge_stage = Ditto.merge(merge_stage, c_stage, 0)
                
                if c_stage == sink_stage:
                    sink_stage = merge_stage
                
                for c in c_stage.children:
                    c.parents.remove(c_stage)
                    c.parents.append(merge_stage)
                    merge_stage.children.append(c)
            
            self.assign(final_stage, 1)
        
        elif obj == 'cost':
            file_size = []
            param_a_list = []
            # Assume each function under Ditto has the same memory size
            if self.storage_mode == 's3':
                for stage in self.workflow.stages:
                    param_a, _ = stage.perf_model.get_params()
                    param_a = math.sqrt(abs(param_a))
                    param_a_list.append(param_a)
                    
                sum_a = sum(param_a_list)
                self.parallelism_ratio = [item / sum_a for item in param_a_list]
            else:
                raise NotImplementedError()

        else:
            raise ValueError('Invalid obj')
        
    def set_config(self, total_parallelism):
        pr = self.parallelism_ratio.copy()
        for idx, stage in enumerate(self.workflow.stages):
            if stage.allow_parallel is False:
                pr[idx] = 0
        pr =(np.array(pr) / np.sum(pr)).tolist()
        num_funcs = [int(item * total_parallelism) for item in pr]
        num_funcs = [item if item > 0 else 1 for item in num_funcs]
        for stage in self.workflow.stages:
            stage.num_func = num_funcs[stage.stage_id]
        self.num_funcs = num_funcs
        

def main():
    parser = argparse.ArgumentParser()

    parser.add_argument('-w', '--workflow', type=str, required=True, help='workfloe name abbreviation, e.g., ml, tpcds, video')
    parser.add_argument('-s', '--scheduler', type=str, default='jolteon', help='scheduler name, e.g., jolteon, ditto, orion, caerus')
    parser.add_argument('-bt', '--bound_type', type=str, default='latency', help='bound type, e.g., latency, cost, also used as obj type for ditto')
    parser.add_argument('-bv', '--bound_value', type=float, default=40, help='bound value')
    parser.add_argument('-l', '--service_level', type=float, default=0.95, help='service level')
    parser.add_argument('-c', '--confidence', type=float, default=0.999, help='confidence')
    parser.add_argument('-p', '--profile', type=int, default=0, help='profile or not, 0 or 1')
    parser.add_argument('-t', '--train', type=int, default=0, help='train or not, 0 or 1')

    args = parser.parse_args()

    workflow_file = ''
    if args.workflow == 'ml':
        workflow_file = 'ML-pipeline.json'
    elif args.workflow == 'tpcds':
        workflow_file = 'tpcds-dsq95.json'
    elif args.workflow == 'video':
        workflow_file = 'Video-analytics.json'
    else:
        raise ValueError('Invalid workflow')
    
    perf_model_type = -1
    if args.scheduler == 'jolteon':
        perf_model_type = 0
    elif args.scheduler == 'orion':
        perf_model_type = 1
    elif args.scheduler == 'ditto' or args.scheduler == 'caerus':
        perf_model_type = 2
    else:
        raise ValueError('Invalid scheduler')

    wf = Workflow(workflow_file, perf_model_type = perf_model_type)

    if args.profile == 1:
        wf.profile()
    elif args.train == 1:
        wf.train_perf_model(wf.metadata_path('profiles'))
        if args.scheduler == 'jolteon':
            scheduler = Jolteon(wf)
            scheduler.store_params_and_samples()
    else:
        wf.train_perf_model(wf.metadata_path('profiles'))
        if args.scheduler == 'jolteon':
            scheduler = Jolteon(wf)
            scheduler.store_params_and_samples()
            scheduler.set_bound(args.bound_type, args.bound_value, args.service_level)
            scheduler.set_confidence(args.confidence)
            scheduler.generate_func_code()

            param_path = wf.metadata_path('params')
            sample_path = wf.metadata_path('samples')
            t0 = time.time()
            scheduler.get_params_and_samples()
            t1 = time.time()
            print('Sample time:', t1-t0, 's\n\n')
            t0 = time.time()
            scheduler.search_config(param_path, sample_path, x_bound=[(1, 32), (0.5, 3.05)])
            t1 = time.time()
            print('Search time:', t1-t0, 's\n\n')
            scheduler.set_config()
        elif args.scheduler == 'orion':
            scheduler = Orion(wf)
            scheduler.comp_ratio()
            scheduler.set_config(32, 25, 0.9)
        elif args.scheduler == 'ditto':
            scheduler = Ditto(wf)
            scheduler.comp_ratio(args.bound_type)
            scheduler.set_config(32)
        elif args.scheduler == 'caerus':
            scheduler = Caerus(wf)
            scheduler.comp_ratio()
            scheduler.set_config(32)

        print('\n\n')
        for stage in scheduler.workflow.stages:
            parent_d = 0
            if not stage.allow_parallel:
                for i in range(len(stage.parents)-1, -1, -1):
                    if stage.parents[i].allow_parallel:
                        parent_d = stage.parents[i].num_func
                        break
            print('Predict for stage', stage.stage_id, ':', 
                  stage.perf_model.predict(num_vcpu=scheduler.num_vcpus[stage.stage_id],
                                           num_func=scheduler.num_funcs[stage.stage_id],
                                           parent_d=parent_d), 
                  stage.perf_model.predict(num_vcpu=scheduler.num_vcpus[stage.stage_id],
                                           num_func=scheduler.num_funcs[stage.stage_id],
                                           parent_d=parent_d, mode='cost'))
        print('\n\n')
        print('Predict time for workflow:', scheduler.workflow.predict())
        print('Predict cost for workflow:', scheduler.workflow.predict('cost'))
        
        # wf.init_stage_status()
        # clear_dir = wf.workflow_name + '/stage'
        # clear_dir = clear_dir.replace('-', '_')
        # clear_data(clear_dir)
        # t0 = time.time()
        # res = wf.lazy_execute()
        # t1 = time.time()
        # print('Time:', t1 - t0)
        # print(res)
        # infos = []
        # time_list = []
        # times_list = []
        # for ids, r in enumerate(res):
        #     l = []
        #     for ids_, result in enumerate(r):
        #         if ids_ == 0:
        #             time_list.append(result)
        #             continue
        #         info = extract_info_from_log(result[1])
        #         infos.append(info)
                
        #         rd = json.loads(result[0])
        #         if 'statusCode' not in rd:
        #             print(rd)
        #         rd = json.loads(rd['body'])
        #         l.append(rd['breakdown'])
        #     times_list.append(l)
        # cost = 0
        # for info in infos:
        #     cost += info['bill']
        # print('Cost:', cost, '$')
        # for idx, t in enumerate(time_list):
        #     print('Stage', idx, 'time:', t)
        #     print(times_list[idx])
        
        wf.close_pools()


if __name__ == '__main__':
    main()

    # Test jolteon
    # wf = Workflow('ML-pipeline.json', perf_model_type = 0)
    # wf.train_perf_model('/home/ubuntu/workspace/chaojin-dev/serverless-bound/profiles/ML-Pipeline_profile.json')

    # scheduler = Jolteon(wf)
    # scheduler.set_bound('latency', 40, 0.95)
    # t0 = time.time()
    # param_path, sample_path = scheduler.store_params_and_samples()
    # t1 = time.time()
    # print('Sample time:', t1-t0, 's\n\n')
    # scheduler.generate_func_code()
    # t0 = time.time()
    # scheduler.search_config(param_path, sample_path)
    # t1 = time.time()
    # print(PCPSolver.sample_size(len(wf.stages), scheduler.risk, 0, scheduler.confidence_error))
    # print('Search time:', t1-t0, 's\n\n')
    # scheduler.set_config()

    # Test ditto
    # wf = Workflow('ML-pipeline.json', perf_model_type = 2)
    # wf.train_perf_model('/home/ubuntu/workspace/chaojin-dev/serverless-bound/profiles/ML-Pipeline_profile.json')
    # scheduler = Ditto(wf)
    # scheduler.comp_ratio()
    # scheduler.set_config(32)
    # print(scheduler.parallelism_ratio)
    # print(scheduler.num_funcs)
    
    # Test orion
    # wf = Workflow('ML-pipeline.json', perf_model_type = 1)
    # wf.train_perf_model('/home/ubuntu/workspace/chaojin-dev/serverless-bound/profiles/ML-Pipeline_profile.json')
    # scheduler = Orion(wf)
    # scheduler.comp_ratio()
    # scheduler.set_config(32, 25, 0.9)
    
    # Test caerus
    # for stage in wf.stages:
    #     print(str(stage.stage_id) + ':' + str(stage.num_func), end=' ')
    # print()
    # t1 = time.time()
    # res = wf.lazy_execute()
    # t2 = time.time()
    # print('Time:', t2 - t1)
    # infos = []
    # time_list = []
    # times_list = []
    # for ids, r in enumerate(res):
    #     l = []
    #     for ids_, result in enumerate(r):
    #         if ids_ == 0:
    #             time_list.append(result)
    #             continue
    #         info = utils.extract_info_from_log(result[1])
    #         infos.append(info)
            
    #         rd = json.loads(result[0])
    #         if 'statusCode' not in rd:
    #             print(rd)
    #         rd = json.loads(rd['body'])
    #         l.append(rd['breakdown'][-1])
    #     times_list.append(l)
    # cost = 0
    # for info in infos:
    #     cost += info['bill']
    # print('Cost:', cost, '$')
    # for idx, t in enumerate(time_list):
    #     print('Stage', idx, 'time:', t)
    #     print(max(times_list[idx]))
    # print('Idea DAG Execution Time:', time_list[0] + time_list[1]\
    #         + time_list[4] + time_list[6] + time_list[7])
    # print('\n\n')

    # wf.close_pools()