import numpy as np
from policy import RandomPolicy, GreedyPolicy
from callbacks import PrintCallbacksManager
from util import RunType
import time


class Runner:
    def __init__(self, run_type, agent, env, nb_steps, nb_steps_ep_max=None, print_rew_cb=None, print_eps_cb=None,
                 benchmark=None, visualize=False, allow_printing=True):
        self.run_type = run_type
        self.agent = agent
        self.env = env

        # Check agent compatibility with env
        action_size = env.action_space.n
        state_shape = env.observation_space.shape
        self.agent.check_env_compatibility(action_size=action_size, state_shape=state_shape)

        self.nb_steps = nb_steps
        self.nb_steps_ep_max = nb_steps_ep_max

        # Make a callbacksmanager
        callbacks = []
        # if save_model_cb is not None:
        #     callbacks.append(save_model_cb)
        if print_rew_cb is not None:
            callbacks.append(print_rew_cb)
        if print_eps_cb is not None:
            callbacks.append(print_eps_cb)
        self.cbmanager = PrintCallbacksManager(callbacks, self.run_type)
        self.print_rew_cb = print_rew_cb
        # self.save_model_cb = save_model_cb

        self.benchmark = benchmark
        self.visualize = visualize
        self.allow_printing = allow_printing

        # Set training benchmark info
        if run_type is RunType.TRAIN:
            self.agent.training_sess_nb_steps_ep_max = nb_steps_ep_max

        assert self.agent.uses_replay is not None, "`uses_replay` is still None, need to set it."

        # This is for PER, i want to set the first some odd errors/priorities for rand fill
        # It will stop at this or whatever the length of the memory is if its less than set_errors_max_step
        self.set_errors_max_step = 500

    def run(self):
        if self.run_type is RunType.TRAIN:
            assert self.agent.currently_used_policy is self.agent.policy, \
                "While training the current policy should be the init one"

        # If it needs to fill the mem and its not already doing that then fill it
        if self.agent.uses_replay is True and self.run_type is RunType.TRAIN:
            self.cbmanager.set_run_type_of_cbs(RunType.RAND_FILL)
            mem_filler = Runner(RunType.RAND_FILL, self.agent, self.env, None, self.nb_steps_ep_max, None, None, None,
                                False, self.allow_printing)
            mem_filler.run()
            self.cbmanager.set_run_type_of_cbs(self.run_type)

        self.print_flag()

        # Now temp replace the pol if rand_fill or test
        self.temp_replace_policy()

        current_total_step = 0
        current_episode = 1  # MUST START AT 1
        current_ep_step, state = self.reset_episode()

        if self.allow_printing and self.run_type is not RunType.RAND_FILL:
            print("Episode {}...".format(current_episode))

        while self.check_loop(current_total_step):

            action = self.agent.act(state)

            if self.visualize:
                self.env.render()

            next_state, reward, done, _ = self.env.step(action)
            # Process the state
            next_state = self.agent.state_processor.process_state(next_state, self.agent.sess, state)

            # Clips reward to [-1.0, 1.0] if clipping is on for the agent
            if self.agent.reward_clipping is True:
                reward = np.clip(reward, -1.0, 1.0)
            if self.print_rew_cb is not None:
                self.print_rew_cb.update(reward)
            if self.benchmark is not None:
                self.benchmark.update(reward)

            if done:
                next_state = None

            self.remember(state, action, reward, next_state, current_total_step)

            self.update_models_and_policy(current_total_step)

            if done or current_ep_step == self.nb_steps_ep_max:
                # End of episode
                self.cbmanager.access_callbacks(self.allow_printing, end_of_episode=True, episode=current_episode)
                if self.benchmark is not None:
                    self.benchmark.update_end_of_ep(current_episode)
                    self.benchmark.output_to_file(False, current_episode, agent_summary=self.agent.summary(),
                                                  runner_summary=self.summary(False, current_total_step))
                current_ep_step, state = self.reset_episode()
                current_episode += 1

                if self.allow_printing and self.run_type is not RunType.RAND_FILL:
                    print("Episode {}...".format(current_episode))
            else:
                state = next_state
                current_ep_step += 1

            # End of step
            self.cbmanager.access_callbacks(self.allow_printing, end_of_step=True, step=current_total_step)
            current_total_step += 1

        # End of run
        self.cbmanager.access_callbacks(self.allow_printing, end_of_run=True)
        if self.benchmark is not None:
            self.benchmark.output_to_file(True, -1, agent_summary=self.agent.summary(),
                                          runner_summary=self.summary(True, current_total_step))
        # Set training benchmark info
        if self.run_type is RunType.TRAIN:
            self.agent.number_of_trained_steps = current_total_step

        if self.allow_printing:
            print("...Done")
        # At very end reset the policy
        self.agent.currently_used_policy = self.agent.policy
        # Refresh cbs in case they are used again
        self.cbmanager.refresh_cbs()

    def print_flag(self):
        if self.allow_printing:
            if self.run_type is RunType.RAND_FILL:
                print("Randomly filling agent memory...")
            elif self.run_type is RunType.TRAIN:
                print("Training agent...")
            elif self.run_type is RunType.TEST:
                print("Testing agent...")

    def temp_replace_policy(self):
        if self.run_type is RunType.RAND_FILL:
            self.agent.currently_used_policy = RandomPolicy()
        if self.run_type is RunType.TEST:
            self.agent.currently_used_policy = GreedyPolicy()

    def reset_episode(self):
        reset_state = self.env.reset()
        return 0, self.agent.state_processor.process_state(reset_state, self.agent.sess, None)

    def check_loop(self, current_total_step):
        if self.run_type is RunType.RAND_FILL:
            if self.agent.memory.is_full() is False:
                return True
            else:
                return False
        if self.run_type is RunType.TRAIN or self.run_type is RunType.TEST:
            if current_total_step == self.nb_steps:
                return False
            else:
                return True

    def remember(self, state, action, reward, next_state, current_step):
        if self.run_type is RunType.TEST:
            return
        # Set the error of the transition
        set_errors = False
        if current_step < self.set_errors_max_step and self.run_type is RunType.RAND_FILL:
            set_errors = True
        self.agent.remember(state, action, reward, next_state, set_errors)

    def update_models_and_policy(self, current_total_step):
        if self.run_type is RunType.RAND_FILL or self.run_type is RunType.TEST:
            return
        if current_total_step % self.agent.replay_period_wait == 0:
            self.agent.update_params()
        self.agent.update_implicit_policy(current_total_step)
        self.agent.check_update_target_model(current_total_step)

    def summary(self, end_of_run, current_step):
        text = ""
        text += "Train Run Info:\n"
        text += "Total number of steps trained: {}\n".format(self.agent.number_of_trained_steps)
        text += "Max number of steps allowed per episode: {}\n\n".format(
            self.agent.training_sess_nb_steps_ep_max if self.agent.training_sess_nb_steps_ep_max is not None else
            'No Limit')

        text += "Test Run Data:\n"
        text += "Environment: {}\n".format(self.env)
        text += "Total number of steps tested: {}\n".format((current_step + 1) if end_of_run is False else current_step)
        text += "Max number of steps allowed per episode: {}\n".format(
            self.nb_steps_ep_max if self.nb_steps_ep_max is not None else 'No Limit')

        return text
