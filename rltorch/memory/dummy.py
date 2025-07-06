import numpy as np
import torch
from torch.utils.data.sampler import WeightedRandomSampler

from .multi_step import MultiStepBuff


class DummyMemory(dict):
    state_keys = ['state', 'next_state']
    np_keys = ['action', 'reward', 'done']
    keys = state_keys + np_keys

    def __init__(self, capacity, state_shape, action_shape, device):
        super(DummyMemory, self).__init__()
        self.capacity = int(capacity)
        self.state_shape = state_shape
        self.action_shape = action_shape
        self.device = device
        self.reset()

    def reset(self):
        for key in self.state_keys:
            self[key] = []

        self['action'] = np.empty(
            (self.capacity, *self.action_shape), dtype=np.float32)
        self['reward'] = np.empty((self.capacity, 1), dtype=np.float32)
        self['done'] = np.empty((self.capacity, 1), dtype=np.float32)

        self._n = 0
        self._p = 0

    def append(self, state, action, reward, next_state, done,
               episode_done=None):
        self._append(state, action, reward, next_state, done)

    def _append(self, state, action, reward, next_state, done):
        self['state'].append(state)
        self['next_state'].append(next_state)
        self['action'][self._p] = action
        self['reward'][self._p] = reward
        self['done'][self._p] = done

        self._n = min(self._n + 1, self.capacity)
        self._p = (self._p + 1) % self.capacity

        self.truncate()

    def truncate(self):
        if len(self) > self.capacity:
            for key in self.state_keys:
                self[key] = self[key][-self.capacity:]

    def sample(self, batch_size):
        indices = np.random.randint(low=0, high=len(self), size=batch_size)
        return self._sample(indices, batch_size)

    def _sample(self, indices, batch_size):
        bias = -self._p if self._n == self.capacity else 0

        states = np.empty(
            (batch_size, *self.state_shape), dtype=np.uint8)
        next_states = np.empty(
            (batch_size, *self.state_shape), dtype=np.uint8)

        for i, index in enumerate(indices):
            _index = np.mod(index+bias, self.capacity)
            states[i, ...] = \
                np.array(self['state'][_index], dtype=np.uint8)
            next_states[i, ...] = \
                np.array(self['next_state'][_index], dtype=np.uint8)

        states = \
            torch.ByteTensor(states).to(self.device).float() / 255.
        next_states = \
            torch.ByteTensor(next_states).to(self.device).float() / 255.
        actions = torch.FloatTensor(self['action'][indices]).to(self.device)
        rewards = torch.FloatTensor(self['reward'][indices]).to(self.device)
        dones = torch.FloatTensor(self['done'][indices]).to(self.device)

        return states, actions, rewards, next_states, dones

    def __len__(self):
        return len(self['state'])

    def get(self):
        return dict(self)

    def load(self, memory):
        for key in self.state_keys:
            self[key].extend(memory[key])

        num_data = len(memory['state'])
        assert len(memory['state']) == len(memory['priority'])
        if self._p + num_data <= self.capacity:
            for key in self.np_keys:
                self[key][self._p:self._p+num_data] = memory[key]
        else:
            mid_index = self.capacity - self._p
            end_index = num_data - mid_index
            for key in self.np_keys:
                self[key][self._p:] = memory[key][:mid_index]
                self[key][:end_index] = memory[key][mid_index:]

        self._n = min(self._n + num_data, self.capacity)
        self._p = (self._p + num_data) % self.capacity
        self.truncate()
        assert self._n == len(self)


class DummyMultiStepMemory(DummyMemory):

    def __init__(self, capacity, state_shape, action_shape, device,
                 gamma=0.99, multi_step=3):
        super(DummyMultiStepMemory, self).__init__(
            capacity, state_shape, action_shape, device)

        self.gamma = gamma
        self.multi_step = int(multi_step)
        if self.multi_step != 1:
            self.buff = MultiStepBuff(maxlen=self.multi_step)

    def append(self, state, action, reward, next_state, done,
               episode_done=False):
        if self.multi_step != 1:
            self.buff.append(state, action, reward)

            if len(self.buff) == self.multi_step:
                state, action, reward = self.buff.get(self.gamma)
                self._append(state, action, reward, next_state, done)

            if episode_done or done:
                self.buff.reset()
        else:
            self._append(state, action, reward, next_state, done)


class DummyPrioritizedMemory(DummyMultiStepMemory):
    state_keys = ['state', 'next_state']
    np_keys = ['action', 'reward', 'done', 'priority']
    keys = state_keys + np_keys

    def __init__(self, capacity, state_shape, action_shape, device, gamma=0.99,
                 multi_step=3, alpha=0.6, beta=0.4, beta_annealing=0.001,
                 epsilon=1e-4):
        super(DummyPrioritizedMemory, self).__init__(
            capacity, state_shape, action_shape, device, gamma, multi_step)
        self.alpha = alpha
        self.beta = beta
        self.beta_annealing = beta_annealing
        self.epsilon = epsilon

    def reset(self):
        super(DummyPrioritizedMemory, self).reset()
        self['priority'] = np.empty((self.capacity, 1), dtype=np.float32)

    def append(self, state, action, reward, next_state, done, error,
               episode_done=False):
        if self.multi_step != 1:
            self.buff.append(state, action, reward)

            if len(self.buff) == self.multi_step:
                state, action, reward = self.buff.get(self.gamma)
                self['priority'][self._p] = self.calc_priority(error)
                self._append(state, action, reward, next_state, done)

            if episode_done or done:
                self.buff.reset()
        else:
            self['priority'][self._p] = self.calc_priority(error)
            self._append(
                state, action, reward, next_state, done)

    def update_priority(self, indices, errors):
        self['priority'][indices] = np.reshape(
            self.calc_priority(errors), (-1, 1))

    def calc_priority(self, error):
        return (np.abs(error) + self.epsilon) ** self.alpha

    def get(self):
        state_dict = {key: self[key] for key in self.state_keys}
        np_dict = {key: self[key][:self._n] for key in self.np_keys}
        state_dict.update(**np_dict)
        return state_dict

    def sample(self, batch_size):
        self.beta = min(1. - self.epsilon, self.beta + self.beta_annealing)
        sampler = WeightedRandomSampler(
            self['priority'][:self._n, 0], batch_size)
        indices = list(sampler)

        batch = self._sample(indices, batch_size)
        priorities = np.array(self['priority'][indices], dtype=np.float32)
        priorities = priorities / np.sum(self['priority'][:self._n])

        weights = (self._n * priorities) ** -self.beta
        weights /= np.max(weights)
        weights = torch.FloatTensor(
            weights).view(batch_size, -1).to(self.device)

        return batch, indices, weights
