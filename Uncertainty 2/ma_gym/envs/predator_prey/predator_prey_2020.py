import copy
import logging
from typing import List, Tuple

import gym
import numpy as np
from PIL import ImageColor
from gym import spaces
from gym.utils import seeding

from ..utils.action_space import MultiAgentActionSpace
from ..utils.observation_space import MultiAgentObservationSpace
from ..utils.draw import draw_grid, fill_cell, draw_circle, write_cell_text

import warnings
warnings.filterwarnings("ignore", category=UserWarning)


logger = logging.getLogger(__name__)

'''
Modified env.
Spiders always start from the top right most corner.
One prey starts from bottom left and the other prey starts from top left.
Both preys travel in opposite directions.
'''


class PredatorPrey(gym.Env):
    """
    Here there are m spiders and one fly moving on a
    2-dimensional grid. During each time period the fly moves
    to some other position according to a given state-dependent
    probability distribution. The spiders, working as a team, aim
    to catch the fly at minimum cost (thus the one-stage cost is
    equal to 1, until reaching the state where the fly is caught,
    at which time the one-stage cost becomes 0). Each spider
    learns the current state (the vector of spiders and fly locations)
    at the beginning of each time period, and either moves to a
    neighboring location or stays where it is. [Bertsekas, 2020]
    """
    metadata = {'render.modes': ['human', 'rgb_array']}

    def __init__(
            self,
            grid_shape=(20, 20), 
            n_agents=2,
            n_preys=2,
            prey_move_probs=(0.485, 0.01, 0.485, 0.01, 0.01),
            # penalty=1,  # initially -0.5; here we assume no penalty for catching the prey solo
            step_cost=-1,
            prey_capture_reward=0,
            max_steps=500):
        
        self._grid_shape = grid_shape
        self.grid_shape = grid_shape
        self.n_agents = n_agents
        self.n_preys = n_preys
        self._max_steps = max_steps
        self._step_count = None
        self._substep_count = 0
        # self._penalty = penalty
        self._step_cost = step_cost
        self._prey_capture_reward = prey_capture_reward
        self.init_prey_pos = {0:[int(self.grid_shape[0]/2),int(self.grid_shape[0]/2)], 1:[int(self.grid_shape[0]/2),int(self.grid_shape[0]/2)]}

        self.action_space = MultiAgentActionSpace([spaces.Discrete(5) for _ in range(self.n_agents)])
        self.agent_pos = {_: None for _ in range(self.n_agents)}
        self.prey_pos = {_: None for _ in range(self.n_preys)}
        self._prey_alive = None

        self._base_grid = self.__create_grid()  # with no agents

        self._agent_dones = [False for _ in range(self.n_agents)]
        self._prey_move_probs = prey_move_probs
        self.viewer = None

        # Returns relative position -> positions of all agents & prey
        self._obs_high = np.array([1., 1.] * n_agents + [1., 1.] * n_preys)
        self._obs_low = np.array([0., 0.] * n_agents + [0., 0.] * n_preys)
        self.observation_space = MultiAgentObservationSpace(
            [spaces.Box(self._obs_low, self._obs_high) for _ in range(self.n_agents)])

        self._total_episode_reward = None
        self.seed()

    def get_action_meanings(self, agent_i=None):
        if agent_i is not None:
            assert agent_i <= self.n_agents
            return [ACTION_MEANING[i] for i in range(self.action_space[agent_i].n)]
        else:
            return [[ACTION_MEANING[i] for i in range(ac.n)] for ac in self.action_space]

    def action_space_sample(self):
        return [agent_action_space.sample() for agent_action_space in self.action_space]

    def __draw_base_img(self):
        self._base_img = draw_grid(self._grid_shape[0], self._grid_shape[1], cell_size=CELL_SIZE, fill='white')

    def __create_grid(self):
        _grid = [[PRE_IDS['empty'] for _ in range(self._grid_shape[1])] for row in range(self._grid_shape[0])]
        return _grid

    # def __init_positions(self):
    #     init_prey_pos = {0:[int(self.grid_shape[0]/2),int(self.grid_shape[0]/2)], 1:[int(self.grid_shape[0]/2),int(self.grid_shape[0]/2)]}
    #     init_agent_pos = {0:[0,self.grid_shape[0]-1], 1:[0,self.grid_shape[0]-1]}
    #     for agent_i in range(self.n_agents):
    #         self.agent_pos[agent_i] = init_agent_pos[agent_i]
            
    #         # while True:
    #         #     pos = [self.np_random.randint(0, self._grid_shape[0] - 1),
    #         #            self.np_random.randint(0, self._grid_shape[1] - 1)]
    #         #     if self._is_cell_vacant(pos, agent_id=agent_i):
    #         #         self.agent_pos[agent_i] = [0,49]
    #         #         break

    #     for prey_i in range(self.n_preys):
    #         self.prey_pos[prey_i] = init_prey_pos[prey_i]

    #     self.__draw_base_img()

    def __init_positions(self):
        for agent_i in range(self.n_agents):
            while True:
                pos = [self.np_random.randint(0, self._grid_shape[0] - 1),
                       self.np_random.randint(0, self._grid_shape[1] - 1)]
                if self._is_cell_vacant(pos, agent_id=agent_i):
                    self.agent_pos[agent_i] = pos
                    break

        for prey_i in range(self.n_preys):
            while True:
                pos = [self.np_random.randint(0, self._grid_shape[0] - 1),
                       self.np_random.randint(0, self._grid_shape[1] - 1)]

                if self._is_cell_vacant(pos, prey_id=prey_i):  # and (self._neighbour_agents(pos)[0] == 0):
                    self.prey_pos[prey_i] = pos
                    break

        self.__draw_base_img()


    def get_agent_obs(self):
        _obs = []

        # all agents' position
        for agent_i in range(self.n_agents):
            pos = self.agent_pos[agent_i]
            #print(pos)

            _agent_i_obs = [pos[0] / (self._grid_shape[0] - 1), pos[1] / (self._grid_shape[1] - 1)]  # coordinates
            #print("Printing Agent's Observations")
            #print(_agent_i_obs)
            _obs.append(_agent_i_obs)

        # all preys' position
        for prey_j in range(self.n_preys):
            pos = self.prey_pos[prey_j]
            _prey_j_obs = [pos[0] / (self._grid_shape[0] - 1), pos[1] / (self._grid_shape[1] - 1)]  # coordinates

            _obs.append(_prey_j_obs)

        # alive status fro prey
        _obs.append([1. if alive else 0. for alive in self._prey_alive])

        #print(_obs)
        # same observations for all agents
        _obs = np.array(_obs).flatten().tolist()
        _obs = [_obs for _ in range(self.n_agents)]

        return _obs

    def reset(self):
        self._total_episode_reward = [0 for _ in range(self.n_agents)]
        self.agent_pos = {}
        self.prey_pos = {}

        self.__init_positions()
        self._step_count = 0
        self._agent_dones = [False for _ in range(self.n_agents)]
        self._prey_alive = [True for _ in range(self.n_preys)]

        return self.get_agent_obs()

    def reset_default(self):
        print("Number of preys",self.n_preys)
        print("Number of agents",self.n_agents)

        assert self.n_preys == 2
        assert self.n_agents == 4
        assert self._grid_shape == (10, 10)

        self._total_episode_reward = [0 for _ in range(self.n_agents)]
        self.agent_pos = {}
        self.prey_pos = {}

        # closer to the second prey
        self.agent_pos[0] = [9, 5]
        self.agent_pos[1] = [9, 6]
        self.agent_pos[2] = [5, 9]
        self.agent_pos[3] = [6, 9]

        # at corners
        self.prey_pos[0] = [0, 0]
        self.prey_pos[1] = [9, 9]

        self.__draw_base_img()

        self._step_count = 0
        self._agent_dones = [False for _ in range(self.n_agents)]
        self._prey_alive = [True for _ in range(self.n_preys)]

        return self.get_agent_obs()

    def reset_from(self, obs):
        self._total_episode_reward = [0 for _ in range(self.n_agents)]
        self.agent_pos = {}
        self.prey_pos = {}

        # agent positions
        for i in range(self.n_agents):
            start_ind = int(i * 2)
            row_pos_scaled, col_pos_scaled = obs[start_ind], obs[start_ind + 1]
            row_pos, col_pos = self._convert_to_pos((row_pos_scaled, col_pos_scaled))
            self.agent_pos[i] = [row_pos, col_pos]

        # prey positions
        for j in range(self.n_preys):
            start_ind = int(self.n_agents * 2 + j * 2)
            row_pos_scaled, col_pos_scaled = obs[start_ind], obs[start_ind + 1]
            row_pos, col_pos = self._convert_to_pos((row_pos_scaled, col_pos_scaled))
            self.prey_pos[j] = [row_pos, col_pos]

        self.__draw_base_img()

        self._step_count = 0
        self._agent_dones = [False for _ in range(self.n_agents)]

        preys_alive = obs[-self.n_preys:]
        self._prey_alive = [bool(prey_alive) for prey_alive in preys_alive]

        return self.get_agent_obs()

    def _convert_to_pos(
            self,
            pos_scaled: Tuple[float, float],
    ) -> Tuple[int, int]:
        grid_row, grid_col = self._grid_shape
        row_pos_scaled, col_pos_scaled = pos_scaled
        row_pos = int(np.round((grid_row - 1) * row_pos_scaled, 0))
        col_pos = int(np.round((grid_col - 1) * col_pos_scaled, 0))
        return row_pos, col_pos

    def __wall_exists(self, pos):
        row, col = pos
        return PRE_IDS['wall'] in self._base_grid[row, col]

    def is_valid(self, pos):
        return (0 <= pos[0] < self._grid_shape[0]) and (0 <= pos[1] < self._grid_shape[1])

    def _is_cell_vacant(self, pos, agent_id=None, prey_id=None):
        assert (agent_id is not None) or (prey_id is not None)

        if not self.is_valid(pos):
            return False

        # check that position does not intersect with the existing agents
        for i, pos_i in self.agent_pos.items():
            if (agent_id is not None) and agent_id == i:
                continue

            if pos_i[0] == pos[0] and pos_i[1] == pos[1]:
                return False

        # check that position does not intersect with the existing preys
        for j, pos_j in self.prey_pos.items():
            if (prey_id is not None) and prey_id == j:
                continue

            if pos_j[0] == pos[0] and pos_j[1] == pos[1]:
                return False

        return True

    def __update_agent_pos(self, agent_i, move):

        curr_pos = copy.copy(self.agent_pos[agent_i])
        next_pos = None
        if move == 0:  # down
            next_pos = [curr_pos[0] + 1, curr_pos[1]]
        elif move == 1:  # left
            next_pos = [curr_pos[0], curr_pos[1] - 1]
        elif move == 2:  # up
            next_pos = [curr_pos[0] - 1, curr_pos[1]]
        elif move == 3:  # right
            next_pos = [curr_pos[0], curr_pos[1] + 1]
        elif move == 4:  # no-op
            pass
        else:
            raise Exception('Action Not found!')

        if next_pos is not None and self.is_valid(next_pos):  # self._is_cell_vacant(next_pos):
            self.agent_pos[agent_i] = next_pos

    def __next_pos(self, curr_pos, move):
        if move == 0:  # down
            next_pos = [curr_pos[0] + 1, curr_pos[1]]
        elif move == 1:  # left
            next_pos = [curr_pos[0], curr_pos[1] - 1]
        elif move == 2:  # up
            next_pos = [curr_pos[0] - 1, curr_pos[1]]
        elif move == 3:  # right
            next_pos = [curr_pos[0], curr_pos[1] + 1]
        elif move == 4:  # no-op
            next_pos = curr_pos
        return next_pos

    def __update_prey_pos(self, prey_i, move):
        curr_pos = copy.copy(self.prey_pos[prey_i])
        if self._prey_alive[prey_i]:
            next_pos = None
            if move == 0:  # down
                next_pos = [curr_pos[0] + 1, curr_pos[1]]
            elif move == 1:  # left
                next_pos = [curr_pos[0], curr_pos[1] - 1]
            elif move == 2:  # up
                next_pos = [curr_pos[0] - 1, curr_pos[1]]
            elif move == 3:  # right
                next_pos = [curr_pos[0], curr_pos[1] + 1]
            elif move == 4:  # no-op
                pass
            else:
                raise Exception('Action Not found!')

            if next_pos is not None and self.is_valid(next_pos):  # self._is_cell_vacant(next_pos):
                self.prey_pos[prey_i] = next_pos
            else:
                # print('pos not updated')
                pass


    def step(self, agents_action):
        self._step_count += 1

        rewards = [self._step_cost for _ in range(self.n_agents)]

        # all agents move
        for agent_i, action in enumerate(agents_action):
            if not (self._agent_dones[agent_i]):
                self.__update_agent_pos(agent_i, action)

        # all preys move
        for prey_i in range(self.n_preys):
            if self._prey_alive[prey_i]:
                _move = self.np_random.choice(len(self._prey_move_probs), 1, p=self._prey_move_probs)[0]
                self.__update_prey_pos(prey_i, _move)

                # recalculate alive status + add reward if caught
                prey_j_pos = self.prey_pos[prey_i]
                for agent_i, agent_pos in self.agent_pos.items():
                    # do not add several rewards if caught by multiple agents
                    if self._prey_alive[prey_i]:
                        if prey_j_pos[0] == agent_pos[0] and prey_j_pos[1] == agent_pos[1]:
                            self._prey_alive[prey_i] = False
                            rewards[agent_i] += self._prey_capture_reward

        if (self._step_count >= self._max_steps) or (True not in self._prey_alive):
            for i in range(self.n_agents):
                self._agent_dones[i] = True

        for i in range(self.n_agents):
            self._total_episode_reward[i] += rewards[i]

        return self.get_agent_obs(), rewards, self._agent_dones, {'prey_alive': self._prey_alive}



    def substep(self, agent_id, action):
        self._substep_count += 1

        # one agent moves
        if not (self._agent_dones[agent_id]):
            self.__update_agent_pos(agent_id, action)

        is_full_step = self._substep_count % self.n_agents == 0

        # Partial Step -> update only one agent's position
        if not is_full_step:
            return self.get_agent_obs(), None, None, {'prey_alive': self._prey_alive}
        # Full Step -> update prey, recalculate rewards
        else:
            self._step_count += 1

            rewards = [self._step_cost for _ in range(self.n_agents)]

            # all preys move
            for prey_i in range(self.n_preys):
                if self._prey_alive[prey_i]:
                    _move = self.np_random.choice(len(self._prey_move_probs), 1, p=self._prey_move_probs)[0]
                    self.__update_prey_pos(prey_i, _move)

                    # recalculate alive status + add reward if caught
                    prey_j_pos = self.prey_pos[prey_i]
                    for agent_i, agent_pos in self.agent_pos.items():
                        # do not add several rewards if caught by multiple agents
                        if self._prey_alive[prey_i]:
                            if prey_j_pos[0] == agent_pos[0] and prey_j_pos[1] == agent_pos[1]:
                                self._prey_alive[prey_i] = False
                                rewards[agent_i] += self._prey_capture_reward

            if (self._step_count >= self._max_steps) or (True not in self._prey_alive):
                for i in range(self.n_agents):
                    self._agent_dones[i] = True

            for i in range(self.n_agents):
                self._total_episode_reward[i] += rewards[i]

            return self.get_agent_obs(), rewards, self._agent_dones, {'prey_alive': self._prey_alive}

    def apply_move(self, agent_id, action):
        # one agent moves
        if not (self._agent_dones[agent_id]):
            self.__update_agent_pos(agent_id, action)

        return self.get_agent_obs()

    def __get_neighbour_coordinates(self, pos):
        neighbours = []
        if self.is_valid([pos[0] + 1, pos[1]]):
            neighbours.append([pos[0] + 1, pos[1]])
        if self.is_valid([pos[0] - 1, pos[1]]):
            neighbours.append([pos[0] - 1, pos[1]])
        if self.is_valid([pos[0], pos[1] + 1]):
            neighbours.append([pos[0], pos[1] + 1])
        if self.is_valid([pos[0], pos[1] - 1]):
            neighbours.append([pos[0], pos[1] - 1])
        return neighbours

    def render(self, mode='rgb_array'):
        img = copy.copy(self._base_img)

        for prey_i in range(self.n_preys):
            if self._prey_alive[prey_i]:
                draw_circle(img, self.prey_pos[prey_i], cell_size=CELL_SIZE, fill=PREY_COLOR)
                write_cell_text(img, text=str(prey_i + 1), pos=self.prey_pos[prey_i], cell_size=CELL_SIZE,
                                fill='white', margin=0.4)

        # for agent_i in range(self.n_agents):
        #     for neighbour in self.__get_neighbour_coordinates(self.agent_pos[agent_i]):
        #         fill_cell(img, neighbour, cell_size=CELL_SIZE, fill=AGENT_NEIGHBORHOOD_COLOR, margin=0.1)
        #     fill_cell(img, self.agent_pos[agent_i], cell_size=CELL_SIZE, fill=AGENT_NEIGHBORHOOD_COLOR, margin=0.1)

        for agent_i in range(self.n_agents):
            draw_circle(img, self.agent_pos[agent_i], cell_size=CELL_SIZE, fill=AGENT_COLOR)
            write_cell_text(img, text=str(agent_i + 1), pos=self.agent_pos[agent_i], cell_size=CELL_SIZE,
                            fill='white', margin=0.4)

        img = np.asarray(img)
        #print(img)
        return img
        # if mode == 'rgb_array':
        #     return img
        # elif mode == 'human':
        #     from gym.envs.classic_control import rendering
        #     if self.viewer is None:
        #         self.viewer = rendering.SimpleImageViewer()
        #     self.viewer.imshow(img)
        #     return self.viewer.isopen

    def seed(self, n=None):
        self.np_random, seed = seeding.np_random(n)
        return [seed]

    def close(self):
        if self.viewer is not None:
            self.viewer.close()
            self.viewer = None

    def apply_action(self, curr_pos, move):
        # curr_pos = copy.copy(self.agent_pos[agent_i])
        if move == 0:  # down
            next_pos = [curr_pos[0] + 1, curr_pos[1]]
        elif move == 1:  # left
            next_pos = [curr_pos[0], curr_pos[1] - 1]
        elif move == 2:  # up
            next_pos = [curr_pos[0] - 1, curr_pos[1]]
        elif move == 3:  # right
            next_pos = [curr_pos[0], curr_pos[1] + 1]
        elif move == 4:  # no-op
            next_pos = [curr_pos[0], curr_pos[1]]
        else:
            raise Exception('Action Not found!')

        return next_pos if self.is_valid(next_pos) else None


AGENT_COLOR = ImageColor.getcolor('blue', mode='RGB')
AGENT_NEIGHBORHOOD_COLOR = (186, 238, 247)
PREY_COLOR = 'red'

CELL_SIZE = 35

WALL_COLOR = 'black'

ACTION_MEANING = {
    0: "DOWN",
    1: "LEFT",
    2: "UP",
    3: "RIGHT",
    4: "NOOP",
}

PRE_IDS = {
    'agent': 'A',
    'prey': 'P',
    'wall': 'W',
    'empty': '0'
}
