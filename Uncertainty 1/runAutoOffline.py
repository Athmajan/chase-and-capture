from time import perf_counter
from concurrent.futures import ThreadPoolExecutor,ProcessPoolExecutor, as_completed
from typing import List, Dict, Tuple
import matplotlib.pyplot as plt

from tqdm import tqdm
import numpy as np
import gym
import torch
import torch.nn as nn
import torch.optim as optim
import ma_gym  # register new envs on import

from src.constants import SpiderAndFlyEnv, deter_5050Prey,deter_5050Prey_Cross_13,deter_5050Prey_agent_0,deter_5050Prey_agent_1, AgentType, \
    QnetType
from src.qnetwork_coordinated import QNetworkCoordinated
from src.agent_seq_rollout import SeqRolloutAgent
from src.agent_rule_based import RuleBasedAgent

import time
import wandb
from src.agent import Agent

import cv2
import warnings

# Suppress the specific gym warning
warnings.filterwarnings("ignore", category=UserWarning)


SEED = 42

M_AGENTS = 2
P_PREY = 2

N_SAMPLES = 50000
BATCH_SIZE = 1024
EPOCHS = 30
N_SIMS = 50
FROM_SCRATCH = True
INPUT_QNET_NAME = deter_5050Prey
OUTPUT_QNET_NAME = deter_5050Prey
BASIS_POLICY_AGENT = AgentType.RULE_BASED
QNET_TYPE = QnetType.BASELINE


def visualize_image(img: np.ndarray, pause_time: float = 0.5):

    if not isinstance(img, np.ndarray):
        raise ValueError("The provided image is not a valid NumPy array")

    plt.imshow(img)
    plt.axis('off') 
    plt.show(block=False) 
    plt.pause(pause_time)  
    plt.close() 


def create_movie_clip(frames: list, output_file: str, fps: int = 10):
    # Assuming all frames have the same shape
    height, width, layers = frames[0].shape
    size = (width, height)
    
    out = cv2.VideoWriter(output_file, cv2.VideoWriter_fourcc(*'mp4v'), fps, size)
    
    for frame in frames:
        out.write(cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
    
    out.release()


def convert_to_x(obs, m_agents, agent_id, action_space, prev_actions):
    # state
    obs_first = np.array(obs, dtype=np.float32).flatten()

    # agent ohe
    agent_ohe = np.zeros(shape=(m_agents,), dtype=np.float32)
    agent_ohe[agent_id] = 1.

    # prev actions
    prev_actions_ohe = np.zeros(shape=(m_agents * action_space.n,), dtype=np.float32)
    for agent_i, action_i in prev_actions.items():
        ohe_action_index = int(agent_i * action_space.n) + action_i
        prev_actions_ohe[ohe_action_index] = 1.

    # combine all
    x = np.concatenate((obs_first, agent_ohe, prev_actions_ohe))

    return x



def getSignallingPolicy(net,obs,m_agents,agent_i,prev_actions,action_space):
    net_dict = {
        0:deter_5050Prey_agent_0,
        1:deter_5050Prey_agent_1,
    }

    net.load_state_dict(torch.load(net_dict[agent_i]))
    net.to(device)
    net.eval()




    agent_ohe = np.zeros(shape=(m_agents,), dtype=np.float32)
    agent_ohe[agent_i] = 1.
    prev_actions_ohe = np.zeros(shape=(m_agents * action_space.n,), dtype=np.float32)
    if agent_i in prev_actions:
        ohe_action_index = int(agent_i * action_space.n) + prev_actions[agent_i]
        prev_actions_ohe[ohe_action_index] = 1.

    obs_first = np.array(obs, dtype=np.float32).flatten()
    x = np.concatenate((obs_first, agent_ohe, prev_actions_ohe))
    xTensor = torch.Tensor(x).view((1,-1))
    best_action = net(xTensor).max(-1)[-1].detach().item()
    return best_action

def getBasePolicy(obs,agent):
    action_id, _ = agent.act_with_info(obs)
    return {agent.id:action_id}



def _simulate_action_par(
            agent_id: int,
            action_id: int,
            n_sims: int,
            obs: List[float],
            m_agents: int,
            agents: List[Agent],
            act_n_signalling,
            act_n_base2,
    ) -> Tuple[int, float]:
        # Memory and CPU load
        # create env
        # run N simulations

        # create env
        env = gym.make(SpiderAndFlyEnv)

        first_act_n = np.empty((m_agents,), dtype=np.int8)

        # optimize for agent i
        for j in range(m_agents):
            #get future agents actions
            if j > agent_id:
                first_act_n[j] = act_n_base2[j]
            elif j< agent_id :
                # get receding agents actions
                first_act_n[j] = act_n_signalling[j]
            elif j == agent_id:
                first_act_n[j] = action_id


        # print(f"Agent_{agent_id}_Action_{action_id}_Signaling Policy_{act_n_signalling}_Base Policy_{act_n_base2}_FirsttepN  {first_act_n}")
        # run N simulations
        avg_total_reward = 0.

        for j in range(n_sims):
            # init env from observation
            env.reset()
            sim_obs_n = env.reset_from(obs)

            # make prescribed first step
            sim_obs_n, sim_reward_n, sim_done_n, sim_info = env.step(first_act_n)
            avg_total_reward += np.sum(sim_reward_n)

            # run simulation
            while not all(sim_done_n):
                sim_act_n = []
                sim_prev_actions = {}
                for agent, sim_obs in zip(agents, sim_obs_n):
                    sim_best_action = agent.act(sim_obs, prev_actions=sim_prev_actions)
                    sim_act_n.append(sim_best_action)
                    sim_prev_actions[agent.id] = sim_best_action

                sim_obs_n, sim_reward_n, sim_done_n, sim_info = env.step(sim_act_n)
                avg_total_reward += np.sum(sim_reward_n)

        env.close()

        avg_total_reward /= len(agents)
        avg_total_reward /= n_sims

        return action_id, avg_total_reward


def actwithinfo(
        action_space,
        _n_workers,
        agent,
        n_sim,
        obs,
        m_agents,
        agents,
        act_n_signalling,
        act_n_base2,
                    ):
    n_actions = action_space.n
    sim_results = []
    with ProcessPoolExecutor(max_workers=_n_workers) as pool:
        futures = []
        for action_id in range(n_actions):
            futures.append(pool.submit(
                _simulate_action_par,
                agent.id,
                action_id,
                n_sim,
                obs,
                m_agents,
                agents,
                act_n_signalling,
                act_n_base2,
            ))
        for f in as_completed(futures):
            res = f.result()
            sim_results.append(res)

    np_sim_results = np.array(sim_results, dtype=np.float32)
    np_sim_results_sorted = np_sim_results[np.argsort(np_sim_results[:, 0])]
    action_q_values = np_sim_results_sorted[:, 1]
    best_action = np.argmax(action_q_values)
    return best_action, action_q_values



from PIL import Image, ImageDraw, ImageFont
import numpy as np


def add_title_to_frame1(frame, logDict):
    
    img = Image.fromarray(frame)
    layer = Image.new('RGB', (max(img.size),int(max(img.size)*1.5)), (255,255,255))
    layer.paste(img)
    font = ImageFont.truetype("arial.ttf", 15)
    draw = ImageDraw.Draw(layer)

    draw.text((int(round(max(img.size)*0.05)), max(img.size)),f"E: {logDict['E']}  S: {logDict['S']}",(0,0,0),font=font)
    draw.text((int(round(max(img.size)*0.05)), int(max(img.size))+15),f"Agent_1 : {logDict['a_0']} __________ : {logDict['aQ_0']}",(0,0,0),font=font)
    draw.text((int(round(max(img.size)*0.05)), int(max(img.size))+30),f"Agent_2 : {logDict['a_1']} __________ : {logDict['aQ_1']}",(0,0,0),font=font)

    # Convert the new image back to a NumPy array
    frame_with_title = np.asarray(layer)
    
    return frame_with_title



def add_title_to_frame(frame, logDict):
    
    ''' 
    logDict = {
                "Agent1_QVals" : all_action_q_values[0].to_list(),
                "Agent2_QVals" : all_action_q_values[1].to_list(),
                "SignalingPolicy" : act_n_signalling,
                "BasePolicy" : act_n_base2,
                "Actions Taken" : act_auto_n,
                "E" : epi,
                "S" : epi_steps,
            }
    '''
    
    img = Image.fromarray(frame)
    layer = Image.new('RGB', (max(img.size),int(max(img.size)*1.5)), (255,255,255))
    layer.paste(img)
    font = ImageFont.truetype("arial.ttf", 10)
    draw = ImageDraw.Draw(layer)

    draw.text((int(round(max(img.size)*0.05)), max(img.size)),f"E: {logDict['E']}  S: {logDict['S']}",(0,0,0),font=font)
    draw.text((int(round(max(img.size)*0.05)), max(img.size)+15),f"Agent_1 : {logDict['Agent1_QVals']}",(0,0,0),font=font)
    draw.text((int(round(max(img.size)*0.05)), max(img.size)+30),f"Agent_2 : {logDict['Agent2_QVals']}",(0,0,0),font=font) 

    draw.text((int(round(max(img.size)*0.05)), max(img.size)+45),f"BasePolicy : {logDict['BasePolicy']}",(0,0,0),font=font) 
    draw.text((int(round(max(img.size)*0.05)), max(img.size)+60),f"SignalingPolicy : {logDict['SignalingPolicy']}",(0,0,0),font=font) 
    draw.text((int(round(max(img.size)*0.05)), max(img.size)+75),f"Actions : {logDict['Actions Taken']}",(0,0,0),font=font) 


    # Convert the new image back to a NumPy array
    frame_with_title = np.asarray(layer)
    
    return frame_with_title




if __name__ == '__main__':
    steps_history = []
    steps_num = 0
    # wandb.init(project="Deter_Flies",name="5050_Deter_On_Random")
    
    _n_workers = 10
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    net = QNetworkCoordinated(M_AGENTS, P_PREY, 5)
    

    env = gym.make(SpiderAndFlyEnv)
    

    for epi in range(EPOCHS):
        # get episode start time
        startTime = time.time()
        # capture episide frames
        frames = []
        # count steps in an episode
        epi_steps = 0

        # collect total reward of an episode
        total_reward = 0


        obs_n = env.reset()
        logDict = {
                "Agent1_QVals" : "",
                "Agent2_QVals" : "",
                "SignalingPolicy" : "",
                "BasePolicy" : "",
                "Actions Taken" : "",
                "E" : epi,
                "S" : epi_steps,
            }
        
        frames.append(add_title_to_frame(env.render(), logDict))

        m_agents = env.n_agents
        p_preys = env.n_preys
        grid_shape = env.grid_shape
        action_space = env.action_space[0]

        done_n = [False] * m_agents

        while not all(done_n):

            # Query Signalling policy from network eval
            prev_actions = {}
            act_n_signalling = []
            with ThreadPoolExecutor(max_workers=_n_workers) as executor:
                futures = [
                    executor.submit(getSignallingPolicy, net, obs, m_agents, agent_i, prev_actions, action_space)
                    for agent_i, obs in enumerate(obs_n)
                ]

                for future in as_completed(futures):
                    act_n_signalling.append(future.result())

            # print(act_n_signalling)



            # Query base policy from base policy (Rule Based)
            agents = [RuleBasedAgent(i, m_agents, p_preys, grid_shape, env.action_space[i]) for i in range(m_agents)]

            act_n_base = []
            with ThreadPoolExecutor(max_workers=_n_workers) as executor:
                futures = [
                    executor.submit(getBasePolicy,obs,agent)
                    for i, (agent,obs) in enumerate(zip(agents,obs_n))
                ]
                for future in as_completed(futures):
                    act_n_base.append(future.result())


            # print(act_n_base)
            act_n_base2 = []
            for agent in range(m_agents):
                for actItem in act_n_base:
                    if agent in actItem:
                        act_n_base2.append(actItem[agent])
                        break





            act_auto_n = []
            all_action_q_values = []
            with ProcessPoolExecutor(max_workers=_n_workers) as executor_outer:
                outer_futures = []
                for i, (agent, obs) in enumerate(zip(agents, obs_n)):
                    outer_futures.append(
                        executor_outer.submit(
                                        actwithinfo,
                                        action_space,
                                        _n_workers,
                                        agent,
                                        N_SIMS,
                                        obs,
                                        m_agents,
                                        agents,
                                        act_n_signalling,
                                        act_n_base2,
                                        ))
                    
                
                    
                for future in as_completed(outer_futures):
                    best_action, action_q_values = future.result()
                    act_auto_n.append(best_action)
                    all_action_q_values.append(action_q_values)

            # print(act_auto_n)


            logDict = {
                "Agent1_QVals" : all_action_q_values[0].tolist(),
                "Agent2_QVals" : all_action_q_values[1].tolist(),
                "SignalingPolicy" : act_n_signalling,
                "BasePolicy" : act_n_base2,
                "Actions Taken" : act_auto_n,
                "E" : epi,
                "S" : epi_steps,
            }
            

            obs_n, reward_n, done_n, info = env.step(act_auto_n)
            epi_steps += 1
            steps_num += 1
            total_reward += np.sum(reward_n)
            frames.append(add_title_to_frame(env.render(), logDict))
            visualize_image(add_title_to_frame(env.render(), logDict))


        # end of an episode. capture time    
        endTime = time.time()
        # wandb.log({'Reward':total_reward, 'episode_steps' : epi_steps,'exeTime':endTime-startTime},step=epi) 
        steps_history.append(epi_steps)
        create_movie_clip(frames,f"Mode11Trained_on_Mode11_{epi}_V2.mp4")

        # if (epi+1) % 10 ==0:
        #     wandb.log({"video": wandb.Video(np.stack(frames,0).transpose(0,3,1,2), fps=20,format="mp4")})


    # wandb.finish()
    env.close()



                
                

     




