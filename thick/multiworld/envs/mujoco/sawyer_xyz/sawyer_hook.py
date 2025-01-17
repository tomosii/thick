import abc
from collections import OrderedDict

import mujoco_py
import numpy as np
import sys

from multiworld.envs.mujoco.mujoco_env import MujocoEnv
from gym.spaces import Box, Dict
from multiworld.core.serializable import Serializable
from multiworld.envs.env_util import (
    get_stat_in_paths,
    create_stats_ordered_dict, get_asset_full_path,
)
from multiworld.core.multitask_env import MultitaskEnv
from multiworld.envs.mujoco.sawyer_xyz.base import SawyerXYZEnv


class SawyerHookEnv(
    SawyerXYZEnv,
    MultitaskEnv,
    Serializable,
    metaclass=abc.ABCMeta,
):
    def __init__(
        self,
        goal_low=(-0.1, 0.45, 0.15, 0),
        goal_high=(0.0, 0.65, .225, 1.0472),
        action_reward_scale=0,
        reward_type='angle_difference',
        indicator_threshold=(.02, .03),
        fix_goal=False,
        fixed_goal=(0, .45, .12, -.25),
        reset_free=False,
        fixed_hand_z=0.12,
        hand_low=(-0.1, 0.45, 0.15),
        hand_high=(0., 0.65, .225),
        target_pos_scale=1,
        target_angle_scale=1,
        min_angle=0,
        max_angle=1.0472,
        version='door',
        **sawyer_xyz_kwargs
    ):
        use_puck = False
        self.door = True
        if version=='puck':
            use_puck = True
            xml_path = 'sawyer_xyz/sawyer_door_pull_hook_puck_arena.xml'
        elif version =='free':
            self.door = False
            xml_path = 'sawyer_xyz/sawyer_hook_target.xml'
        else:
            xml_path = 'sawyer_xyz/sawyer_door_pull_hook.xml'
        self.quick_init(locals())
        self.model_name = get_asset_full_path(xml_path)
        SawyerXYZEnv.__init__(
            self,
            self.model_name,
            hand_low=hand_low,
            hand_high=hand_high,
            **sawyer_xyz_kwargs
        )
        MultitaskEnv.__init__(self)
        # self.initialize_camera(camera)
        self.reward_type = reward_type
        self.indicator_threshold = indicator_threshold

        self.fix_goal = fix_goal
        self.fixed_goal = np.array(fixed_goal)
        self.goal_space = Box(np.array(goal_low), np.array(goal_high), dtype=np.float32)
        self._state_goal = None
        self.fixed_hand_z = fixed_hand_z
        self._use_puck = use_puck

        self.action_space = Box(np.array([-1, -1, -1]), np.array([1, 1, 1]), dtype=np.float32)
        self.state_space = Box(
            np.concatenate((hand_low, [min_angle])),
            np.concatenate((hand_high, [max_angle])),
            dtype=np.float32,
        )
        self.observation_space = Dict([
            ('observation', self.state_space),
            ('desired_goal', self.goal_space),
            ('achieved_goal', self.state_space),
            ('state_observation', self.state_space),
            ('state_desired_goal', self.goal_space),
            ('state_achieved_goal', self.state_space),
        ])
        self.action_reward_scale = action_reward_scale
        self.target_pos_scale = target_pos_scale
        self.target_angle_scale = target_angle_scale
        self.reset_free = reset_free
        if self.door:
            self.door_angle_idx = self.model.get_joint_qpos_addr('doorjoint')
        else:
            self.door_angle_idx = 0
        #ensure env does not start in weird positions
        self.reset_free = True
        self.reset()
        self.reset_free = reset_free
        self.eef_init_pos = None

    def viewer_setup(self):
        self.viewer.cam.trackbodyid = -1
        self.viewer.cam.lookat[0] = -.2
        self.viewer.cam.lookat[1] = .55
        self.viewer.cam.lookat[2] =  0.6
        self.viewer.cam.distance = 0.25
        self.viewer.cam.elevation = -60
        self.viewer.cam.azimuth = 360

    def step(self, action):
        self.set_xyz_action(action)
        u = np.zeros(7)
        self.do_simulation(u, self.frame_skip)
        info = self._get_info()
        ob = self._get_obs()
        reward = self.compute_reward(action, ob)
        done = False
        return ob, reward, done, info

    def _get_obs(self):
        pos = self.get_endeff_pos()
        if self.door:
            angle = self.get_door_angle()
            flat_obs = np.concatenate((pos, angle))
        else:
            flat_obs = pos
        return dict(
            observation=flat_obs,
            desired_goal=self._state_goal,
            achieved_goal=flat_obs,
            state_observation=flat_obs,
            state_desired_goal=self._state_goal,
            state_achieved_goal=flat_obs,
        )

    def _get_info(self):
        if self.door:
            angle_diff = np.abs(self.get_door_angle() - self._state_goal[-1])[0]
        else:
            return dict(
            angle_difference=1,
            angle_success=0,
            hand_distance=1,
            hand_success=0,
            total_distance=1
            )
        hand_dist = np.linalg.norm(self.get_endeff_pos() - self._state_goal[:3])
        info = dict(
            angle_difference=angle_diff,
            angle_success=(angle_diff < self.indicator_threshold[0]).astype(
                float),
            hand_distance=hand_dist,
            hand_success=(hand_dist < self.indicator_threshold[1]).astype(
                float),
            total_distance=angle_diff + hand_dist
        )
        return info

    def get_door_angle(self):
        return np.array([self.data.get_joint_qpos('doorjoint')])

    def _set_puck_xyz(self, pos):
        qpos = self.data.qpos.flat.copy()
        qvel = self.data.qvel.flat.copy()
        qpos[8:11] = pos.copy()
        qvel[8:15] = 0
        self.set_state(qpos, qvel)

    def get_puck_distance(self):
        if not self._use_puck:
            return -1.0
        puck = self.data.get_body_xpos('puck').copy()
        target = self.data.get_site_xpos('target').copy()
        return np.linalg.norm(target-puck)

    def get_target(self):
        target = self.data.get_site_xpos('orig-target-site').copy()
        return target

    @property
    def endeff_id(self):
        return self.model.body_names.index('leftclaw')

    def compute_rewards(self, actions, obs):
        achieved_goals = obs['state_achieved_goal']
        desired_goals = obs['state_desired_goal']
        actual_angle = achieved_goals[:, -1]
        goal_angle = desired_goals[:, -1]
        pos = achieved_goals[:, :3]
        goal_pos = desired_goals[:, :3]
        angle_diff = np.abs(actual_angle - goal_angle)
        pos_dist = np.linalg.norm(pos - goal_pos, axis=1)
        if self.reward_type == 'angle_diff_and_hand_distance':
            r = - (
                angle_diff * self.target_angle_scale
                + pos_dist * self.target_pos_scale
            )
        elif self.reward_type == 'angle_difference':
            r = - angle_diff * self.target_angle_scale

        elif self.reward_type == 'hand_success':
            r = -(angle_diff > self.indicator_threshold[0] or pos_dist >
                  self.indicator_threshold[1]).astype(float)
        else:
            raise NotImplementedError("Invalid/no reward type.")
        return r

    def reset_model(self):
        self._reset_hand()
        if self.door:
            self._set_door_pos(0)
            #if not self.reset_free:
            #    self._reset_hand()
            #    self._set_door_pos(0)
        goal = self.sample_goal()
        self.set_goal(goal)
        self.reset_mocap_welds()
        self.eef_init_pos = self.get_endeff_pos()
        return self._get_obs()

    def get_endeff_init_pos(self):
        return self.eef_init_pos

    def reset(self):
        # super.reset() does not account for reset-free logic.
        ob = self.reset_model()
        if self._use_puck:
            self._set_puck_xyz(pos=np.array([-0.16, 0.625, -0.085]))
        if self.viewer is not None:
            self.viewer_setup()
        return ob

    def _reset_hand(self):
        velocities = self.data.qvel.copy()
        angles = self.data.qpos.copy()
        # Do this to make sure the robot isn't in some weird configuration.
        angles[:7] = self.init_arm_angles
        self.set_state(angles.flatten(), velocities.flatten())
        if self.reset_free:
            self._set_hand_pos(np.array([-0.002, 0.45, 0.15]))
        else:
            self._set_hand_pos(np.array([-.05, .635,  .225]))

    def _set_hand_pos(self, pos):
        for _ in range(10):
            self.data.set_mocap_pos('mocap', pos)
            self.data.set_mocap_quat('mocap', np.array([1, 0, 1, 0]))
            self.do_simulation(None, self.frame_skip)
    @property
    def init_arm_angles(self):
        return [ 1.7244448, -0.92036369,  0.10234232,  2.11178144,  2.97668632, -0.38664629, 0.54065733]

    def _set_door_pos(self, pos):
        qpos = self.data.qpos.copy()
        qvel = self.data.qvel.copy()
        qpos[self.door_angle_idx] = pos
        qvel[self.door_angle_idx] = 0
        self.set_state(qpos.flatten(), qvel.flatten())

    ''' Multitask Functions '''

    @property
    def goal_dim(self):
        return 4

    def set_goal(self, goal):
        self._state_goal = goal['state_desired_goal']

    def sample_goals(self, batch_size):
        if self.fix_goal:
            goals = np.repeat(
                self.fixed_goal.copy()[None],
                batch_size,
                0
            )
        else:
            goals = np.random.uniform(
                self.goal_space.low,
                self.goal_space.high,
                size=(batch_size, self.goal_space.low.size),
            )
        return {
            'desired_goal': goals,
            'state_desired_goal': goals,
        }

    def set_to_goal_angle(self, angle):
        self._state_goal = angle.copy()
        qpos = self.data.qpos.flat.copy()
        qvel = self.data.qvel.flat.copy()
        qpos[-1] = angle.copy()
        qvel[-1] = 0
        self.set_state(qpos, qvel)

    def set_to_goal_pos(self, xyz):
        for _ in range(10):
            self.data.set_mocap_pos('mocap', np.array(xyz))
            self.data.set_mocap_quat('mocap', np.array([1, 0, 1, 0]))
            u = np.zeros(7)
            self.do_simulation(u, self.frame_skip)

    def get_goal(self):
        return {
            'desired_goal': self._state_goal,
            'state_desired_goal': self._state_goal,
        }

    def set_to_goal(self, goal):
        print("Cant do it")
        #raise NotImplementedError("Hard to do because what if the hand is in "
        #                          "the door? Use presampled goals.")

    def get_diagnostics(self, paths, prefix=''):
        statistics = OrderedDict()
        for stat_name in [
            'angle_difference',
            'angle_success',
            'hand_distance',
            'hand_success',
            'total_distance',
        ]:
            stat_name = stat_name
            stat = get_stat_in_paths(paths, 'env_infos', stat_name)
            statistics.update(create_stats_ordered_dict(
                '%s%s' % (prefix, stat_name),
                stat,
                always_show_all_stats=True,
            ))
            statistics.update(create_stats_ordered_dict(
                'Final %s%s' % (prefix, stat_name),
                [s[-1] for s in stat],
                always_show_all_stats=True,
            ))
        return statistics

    def get_env_state(self):
        base_state = super().get_env_state()
        goal = self._state_goal.copy()
        return base_state, goal

    def set_env_state(self, state):
        base_state, goal = state
        super().set_env_state(base_state)
        self._state_goal = goal
