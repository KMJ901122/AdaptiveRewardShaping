import os
import time
from collections import deque
import pickle
from tempfile import TemporaryFile

from baselines.ddpg.ddpg_learner import DDPG
from baselines.ddpg.models import Actor, Critic
from baselines.ddpg.memory import Memory
from baselines.ddpg.noise import AdaptiveParamNoiseSpec, NormalActionNoise, OrnsteinUhlenbeckActionNoise
from baselines.common import set_global_seeds
import baselines.common.tf_util as U

from baselines import logger
import numpy as np

try:
    from mpi4py import MPI
except ImportError:
    MPI = None


def eval_model(agent, eval_env, noise_level, env_name="cheetah", gamma=0.99, eval_nbr = 5):
    temp_eval_success = []
    temp_eval_reward = []
    temp_eval_partial_success = []
    temp_eval_epi_length = []
    max_action = eval_env.action_space.high
    for _ in range(eval_nbr):
        eval_obs = eval_env.reset()
        nenvs_eval = eval_obs.shape[0]
        total_eval_reward = 0
        eval_success = 0
        eval_step = 0
        eval_done = False
        # for water world case, initial state is 1, and success state is 0
        eval_episode_reward = 0
        prev_rm_state = 0
        
        while eval_step < 1000:
            eval_action, _, _, _ = agent.step(eval_obs, apply_noise=False, compute_Q=False)
            action = max_action * eval_action

            if np.random.rand() < float(noise_level):
                action += np.random.uniform(-0.1, 0.1)
                action = np.clip(action, -1., 1.)
            
            eval_obs, eval_r, eval_done, eval_info = eval_env.step(action)
            eval_r = 0.
            
            rm_state = eval_info[0]["rm_state"]
            u1 = prev_rm_state
            u2 = rm_state
            if u1 != u2:
                print("u1", u1)
                print("u2", u2)

            if u1 == 0 and u2 == 1:
                eval_r = 1.
            elif u1 == 1 and u2 == 2:
                eval_r = 1.
            elif u1 == 2 and u2 == 3:
                eval_r = 1.
            elif u1 == 3 and u2 == 4:
                eval_r = 1.
            elif u1 == 4 and (u2 == -1 or u2 == 5):
                eval_r = 1.
            if u2 == -1 or u2 == 5:
                eval_success = 1

            eval_episode_reward += gamma ** eval_step * eval_r
            eval_step += 1
            prev_rm_state = rm_state

        total_eval_reward += eval_episode_reward
        temp_eval_reward.append(eval_episode_reward)
        temp_eval_success.append(eval_success)
        temp_eval_epi_length.append(eval_step)
        temp_eval_partial_success.append(rm_state)
        
    return temp_eval_success, temp_eval_reward, temp_eval_partial_success, temp_eval_epi_length


def learn(network, env,
          seed=None,
          use_crm=True,
          use_rs=False,
          total_timesteps=None,
          nb_epochs=None, # with default settings, perform 1M steps total
          nb_epoch_cycles=20,
          nb_rollout_steps=100,
          reward_scale=1.0,
          render=False,
          render_eval=False,
          noise_type='adaptive-param_0.2',
          normalize_returns=False,
          normalize_observations=True,
          critic_l2_reg=1e-2,
          actor_lr=1e-4,
          critic_lr=1e-3,
          popart=False,
          gamma=0.99,
          clip_norm=None,
          nb_train_steps=50, # per epoch cycle and MPI worker
          nb_eval_steps=1000,
          batch_size=64, # per MPI worker
          tau=0.01,
          eval_env=None,
          param_noise_adaption_interval=50,
          eval_freq=1000,
          eval_nbr=5,
          env_name="cheetah",
          missing=False,
          noise_level:float = 0.,
          test: bool = False,
          **network_kwargs):
    #TODO: im_missing True ...
    use_crm = True
    set_global_seeds(seed)

    if total_timesteps is not None:
        assert nb_epochs is None
        # 2000000
        # 2000
        # nb epochs = 1000
        # np epoch cycles = 20
        nb_epochs = int(total_timesteps) // (nb_epoch_cycles * nb_rollout_steps)
    else:
        nb_epochs = 500

    if MPI is not None:
        rank = MPI.COMM_WORLD.Get_rank()
    else:
        rank = 0

    nb_actions = env.action_space.shape[-1]
    assert (np.abs(env.action_space.low) == env.action_space.high).all()  # we assume symmetric actions.


    limit = int(1e6)
    if use_crm:
        rm_states  = env.envs[0].get_num_rm_states()
        batch_size = rm_states*batch_size
        limit = rm_states*int(1e6)


    memory = Memory(limit=limit, action_shape=env.action_space.shape, observation_shape=env.observation_space.shape)
    critic = Critic(network=network, **network_kwargs)
    actor = Actor(nb_actions, network=network, **network_kwargs)

    action_noise = None
    param_noise = None
    if noise_type is not None:
        for current_noise_type in noise_type.split(','):
            current_noise_type = current_noise_type.strip()
            if current_noise_type == 'none':
                pass
            elif 'adaptive-param' in current_noise_type:
                _, stddev = current_noise_type.split('_')
                param_noise = AdaptiveParamNoiseSpec(initial_stddev=float(stddev), desired_action_stddev=float(stddev))
            elif 'normal' in current_noise_type:
                _, stddev = current_noise_type.split('_')
                action_noise = NormalActionNoise(mu=np.zeros(nb_actions), sigma=float(stddev) * np.ones(nb_actions))
            elif 'ou' in current_noise_type:
                _, stddev = current_noise_type.split('_')
                action_noise = OrnsteinUhlenbeckActionNoise(mu=np.zeros(nb_actions), sigma=float(stddev) * np.ones(nb_actions))
            else:
                raise RuntimeError('unknown noise type "{}"'.format(current_noise_type))

    max_action = env.action_space.high
    logger.info('scaling actions by {} before executing in env'.format(max_action))

    agent = DDPG(actor, critic, memory, env.observation_space.shape, env.action_space.shape,
        gamma=gamma, tau=tau, normalize_returns=normalize_returns, normalize_observations=normalize_observations,
        batch_size=batch_size, action_noise=action_noise, param_noise=param_noise, critic_l2_reg=critic_l2_reg,
        actor_lr=actor_lr, critic_lr=critic_lr, enable_popart=popart, clip_norm=clip_norm,
        reward_scale=reward_scale)
    logger.info('Using agent with the following configuration:')
    logger.info(str(agent.__dict__.items()))

    eval_episode_rewards_history = deque(maxlen=100)
    episode_rewards_history = deque(maxlen=100)
    sess = U.get_session()
    # Prepare everything.
    agent.initialize(sess)
    sess.graph.finalize()

    agent.reset()

    obs = env.reset()
    if eval_env is not None:
        eval_obs = eval_env.reset()
    nenvs = obs.shape[0]

    episode_reward = np.zeros(nenvs, dtype = np.float32) #vector
    episode_step = np.zeros(nenvs, dtype = int) # vector
    episodes = 0 #scalar
    t = 0 # scalar

    epoch = 0

    ###################################################
    # Success rate and parital success rate ###########
    ###################################################
    successes = []
    partial_successes = []
    episode_lengths = []
    episode_rewards = []

    start_time = time.time()
    
    
    epoch_episode_rewards = []
    epoch_episode_steps = []
    epoch_actions = []
    epoch_qs = []
    epoch_episodes = 0
        
    successes = [0]
    partial_successes = [0]
    episode_lengths = [1000]
    my_episode_rewards = [0]
    # 1000
    for epoch in range(nb_epochs):
        # 20
        for cycle in range(nb_epoch_cycles):
            # Perform rollouts.
            if nenvs > 1:
                # if simulating multiple envs in parallel, impossible to reset agent at the end of the episode in each
                # of the environments, so resetting here instead
                agent.reset()
            # 100
            for t_rollout in range(nb_rollout_steps):
                # Predict next action.
                action, q, _, _ = agent.step(obs, apply_noise=True, compute_Q=True)

                # Execute next action.
                if rank == 0 and render:
                    env.render()

                # max_action is of dimension A, whereas action is dimension (nenvs, A) - the multiplication gets broadcasted to the batch
                new_obs, r, done, info = env.step(max_action * action)  # scale for execution in env (as far as DDPG is concerned, every action is in [-1, 1])
                # note these outputs are batched from vecenv

                t += 1
                if rank == 0 and render:
                    env.render()
                episode_reward += r
                episode_step += 1

                # Book-keeping.
                epoch_actions.append(action)
                epoch_qs.append(q)

                # Adding counterfactual experience from the reward machines
                if nenvs >= 1:
                    if not(use_crm or use_rs):
                        # Standard DDPG
                        agent.store_transition(obs, action, r, new_obs, done) #the batched data will be unrolled in memory.py's append.
                    else:
                        # Adding crm and/or reward shaping to DDPG
                        if use_crm:
                            experiences = info[0]["crm-experience"]
                        else:
                            experiences = [(obs, action, info[0]["rs-reward"], new_obs, done)]

                        for _obs, _action, _r, _new_obs, _done in experiences:
                            _obs.shape     = obs.shape
                            _action.shape  = action.shape
                            _new_obs.shape = new_obs.shape
                            _r             = np.array([_r])
                            _done          = np.array([_done])
                            agent.store_transition(_obs, _action, _r, _new_obs, _done) #the batched data will be unrolled in memory.py's append.
                else:
                    assert False, "We have not implemented crm for nenvs > 1 yet"

                obs = new_obs

                for d in range(len(done)):
                    if done[d]:
                        # Episode done.
                        epoch_episode_rewards.append(episode_reward[d])
                        episode_rewards_history.append(episode_reward[d])
                        epoch_episode_steps.append(episode_step[d])
                        episode_reward[d] = 0.
                        episode_step[d] = 0
                        epoch_episodes += 1
                        episodes += 1
                        if nenvs == 1:
                            agent.reset()
                
                
                

            # Train.
            epoch_actor_losses = []
            epoch_critic_losses = []
            epoch_adaptive_distances = []
            for t_train in range(nb_train_steps):
                # Adapt param noise, if necessary.
                if memory.nb_entries >= batch_size and t_train % param_noise_adaption_interval == 0:
                    distance = agent.adapt_param_noise()
                    epoch_adaptive_distances.append(distance)

                cl, al = agent.train()
                epoch_critic_losses.append(cl)
                epoch_actor_losses.append(al)
                agent.update_target_net()

            if t % eval_freq == 0:
                temp_eval_success, temp_eval_reward, temp_eval_partial_success, temp_eval_epi_length = eval_model(agent, eval_env, noise_level, env_name, gamma, eval_nbr)
                
                total_eval_reward = sum(temp_eval_reward) / len(temp_eval_reward)
                print("step: {} and reward: {}".format(t, total_eval_reward))
                my_episode_rewards.append(temp_eval_reward)
                episode_lengths.append(temp_eval_epi_length)
                successes.append(temp_eval_success)
                partial_successes.append(temp_eval_partial_success)


        if MPI is not None:
            mpi_size = MPI.COMM_WORLD.Get_size()
        else:
            mpi_size = 1

        # Log stats.
        # XXX shouldn't call np.mean on variable length lists
        duration = time.time() - start_time
        stats = agent.get_stats()
        combined_stats = stats.copy()
        # combined_stats['rollout/return'] = np.mean(epoch_episode_rewards)
        # combined_stats['rollout/return_std'] = np.std(epoch_episode_rewards)
        # combined_stats['rollout/return_history'] = np.mean(episode_rewards_history)
        # combined_stats['rollout/return_history_std'] = np.std(episode_rewards_history)
        # combined_stats['rollout/episode_steps'] = np.mean(epoch_episode_steps)
        # combined_stats['rollout/actions_mean'] = np.mean(epoch_actions)
        # combined_stats['rollout/Q_mean'] = np.mean(epoch_qs)
        # combined_stats['train/loss_actor'] = np.mean(epoch_actor_losses)
        # combined_stats['train/loss_critic'] = np.mean(epoch_critic_losses)
        # combined_stats['train/param_noise_distance'] = np.mean(epoch_adaptive_distances)
        # combined_stats['total/duration'] = duration
        # combined_stats['total/steps_per_second'] = float(t) / float(duration)
        # combined_stats['total/episodes'] = episodes
        # combined_stats['rollout/episodes'] = epoch_episodes
        # combined_stats['rollout/actions_std'] = np.std(epoch_actions)

        # Evaluation statistics.
        # if eval_env is not None:
        #     combined_stats['eval/return'] = eval_episode_rewards
        #     combined_stats['eval/return_history'] = np.mean(eval_episode_rewards_history)
        #     combined_stats['eval/Q'] = eval_qs
        #     combined_stats['eval/episodes'] = len(eval_episode_rewards)

        # def as_scalar(x):
        #     if isinstance(x, np.ndarray):
        #         assert x.size == 1
        #         return x[0]
        #     elif np.isscalar(x):
        #         return x
        #     else:
        #         raise ValueError('expected scalar, got %s'%x)

        # combined_stats_sums = np.array([ np.array(x).flatten()[0] for x in combined_stats.values()])
        # if MPI is not None:
        #     combined_stats_sums = MPI.COMM_WORLD.allreduce(combined_stats_sums)

        # combined_stats = {k : v / mpi_size for (k,v) in zip(combined_stats.keys(), combined_stats_sums)}

        # # Total statistics.
        # combined_stats['total/epochs'] = epoch + 1
        # combined_stats['total/steps'] = t

        # for key in sorted(combined_stats.keys()):
        #     logger.record_tabular(key, combined_stats[key])

        # if rank == 0:
        #     logger.dump_tabular()
        # logger.info('')
        # logdir = logger.get_dir()
        # if rank == 0 and logdir:
        #     if hasattr(env, 'get_state'):
        #         with open(os.path.join(logdir, 'env_state.pkl'), 'wb') as f:
        #             pickle.dump(env.get_state(), f)
        #     if eval_env and hasattr(eval_env, 'get_state'):
        #         with open(os.path.join(logdir, 'eval_env_state.pkl'), 'wb') as f:
        #             pickle.dump(eval_env.get_state(), f)

    env_name = "cheetah"
    if use_crm and not use_rs:
        save_path = "./results/" + env_name + "_results/crm"
    elif not use_crm and use_rs:
        save_path = "./results/" + env_name + "_results/rs"
    elif use_crm and use_rs:
        save_path = "./results/" + env_name + "_results/crm_rs"

    if bool(missing):
        save_path += "_missing"

    if noise_level > 0:
        save_path += "_noise_" + str(noise_level)


    if not os.path.exists(save_path):
        os.mkdir(save_path)

    np.savez(
        save_path + "/" + str(seed),
        successes=successes,
        partial_successes=partial_successes,
        results=my_episode_rewards,
        ep_lengths=episode_lengths
            )