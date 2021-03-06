#!/usr/bin/env python3
import functools
import os
from multiprocessing import Process
from baselines import logger
from mpi4py import MPI
import mpi_util
import tf_util
from cmd_util import make_atari_env, arg_parser
from policies.cnn_gru_policy_dynamics import CnnGruPolicy
from policies.cnn_policy_param_matched import CnnPolicy
from ppo_agent import PpoAgent
from utils import set_global_seeds
from vec_env import VecFrameStack
from copy import deepcopy
import datetime
from baselines.common.vec_env import VecNormalize
from baselines.common.cmd_util import make_vec_env


def make_mujoco_env(env_id, seed, reward_scale=1.0):
    env = make_vec_env(env_id, "mujoco", num_env=1, seed=seed, reward_scale=reward_scale,
                       flatten_dict_observations=True)
    env = VecNormalize(env)
    return env


def train(*, origin_paper, env_id, num_env, hps, num_timesteps, seed):
    # venv = VecFrameStack(
    #     make_atari_env(env_id, num_env, seed, wrapper_kwargs=dict(),
    #                    start_index=num_env * MPI.COMM_WORLD.Get_rank(),
    #                    max_episode_steps=hps.pop('max_episode_steps')),
    #     hps.pop('frame_stack'))
    # # venv.score_multiple = {'Mario': 500,
    # #                        'MontezumaRevengeNoFrameskip-v4': 100,
    # #                        'GravitarNoFrameskip-v4': 250,
    # #                        'PrivateEyeNoFrameskip-v4': 500,
    # #                        'SolarisNoFrameskip-v4': None,
    # #                        'VentureNoFrameskip-v4': 200,
    # #                        'PitfallNoFrameskip-v4': 100,
    # #                        }[env_id]
    # venv.score_multiple = 1
    # venv.record_obs = True if env_id == 'SolarisNoFrameskip-v4' else False
    venv = make_mujoco_env(env_id, seed, reward_scale=1.0)
    setattr(venv, "score_multiple", 1.0)
    setattr(venv, "record_obs", False)
    ob_space = venv.observation_space
    ac_space = venv.action_space
    gamma = hps.pop('gamma')
    policy = {'rnn': CnnGruPolicy,
              'cnn': CnnPolicy}[hps.pop('policy')]

    agent = PpoAgent(
        scope='ppo',
        ob_space=ob_space,
        ac_space=ac_space,
        stochpol_fn=functools.partial(
                policy,
                scope='pol',
                ob_space=ob_space,
                ac_space=ac_space,
                update_ob_stats_independently_per_gpu=hps.pop('update_ob_stats_independently_per_gpu'),
                proportion_of_exp_used_for_predictor_update=hps.pop('proportion_of_exp_used_for_predictor_update'),
                dynamics_bonus = hps.pop("dynamics_bonus")
            ),
        gamma=gamma,
        gamma_ext=hps.pop('gamma_ext'),
        lam=hps.pop('lam'),
        nepochs=hps.pop('nepochs'),
        nminibatches=hps.pop('nminibatches'),
        lr=hps.pop('lr'),
        cliprange=hps.pop('cliprange'),
        nsteps=hps.pop('nsteps'),
        ent_coef=hps.pop('ent_coef'),
        max_grad_norm=hps.pop('max_grad_norm'),
        use_news=hps.pop("use_news"),
        comm=MPI.COMM_WORLD if MPI.COMM_WORLD.Get_size() > 1 else None,
        update_ob_stats_every_step=hps.pop('update_ob_stats_every_step'),
        int_coeff=hps.pop('int_coeff'),
        ext_coeff=hps.pop('ext_coeff'),
        origin_paper=origin_paper,
    )
    agent.start_interaction([venv])
    if hps.pop('update_ob_stats_from_random_agent'):
        agent.collect_random_statistics(num_timesteps=128*10)
    if len(hps) != 0:
        logger.info("warning!Unused hyperparameters: %s" % list(hps.keys()))

    counter = 0
    while True:
        info = agent.step()
        if info['update']:
            logger.logkvs(info['update'])
            logger.dumpkvs()
            counter += 1
        if agent.I.stats['tcount'] > num_timesteps:
            break

    agent.stop_interaction()


def add_env_params(parser):
    parser.add_argument('--env', help='environment ID', default='HalfCheetah-v2')
    parser.add_argument('--seed', help='RNG seed', type=int, default=0)
    parser.add_argument('--max_episode_steps', type=int, default=4500)


def main(args):
    server = args.server
    origin_paper = args.origin_paper
    if not origin_paper:
        args.nminibatches = 32
        args.nsteps = 2048
        args.max_grad_norm = 0.5
        args.lr = 3e-4
        args.ent_coef = 0
        args.cliprange = 0.2

    if not server:
        path = "logs"
    else:
        path = os.path.join("/home", "lizn", "openai", "rnd",
                            datetime.datetime.now().strftime("ppo-rnd-{}-%Y-%m-%d-%H-%M-%S-%f".format(args.env)))
    logger.configure(dir=path, format_strs=['stdout', 'log', 'csv'] if MPI.COMM_WORLD.Get_rank() == 0 else [])
    if MPI.COMM_WORLD.Get_rank() == 0:
        with open(os.path.join(logger.get_dir(), 'experiment_tag.txt'), 'w') as f:
            f.write(args.tag)

    if server:
        mpi_util.setup_mpi_gpus()

    seed = 10000 * args.seed + MPI.COMM_WORLD.Get_rank()
    set_global_seeds(seed)

    hps = dict(
        frame_stack=4,
        ent_coef=args.ent_coef,
        nminibatches=args.nminibatches,
        nepochs=4 if origin_paper else 10,
        lr=args.lr,
        max_grad_norm=args.max_grad_norm,
        use_news=args.use_news,
        gamma=args.gamma,
        gamma_ext=args.gamma_ext,
        max_episode_steps=args.max_episode_steps,
        lam=args.lam,
        update_ob_stats_every_step=args.update_ob_stats_every_step,
        update_ob_stats_independently_per_gpu=args.update_ob_stats_independently_per_gpu,
        update_ob_stats_from_random_agent=args.update_ob_stats_from_random_agent,
        proportion_of_exp_used_for_predictor_update=args.proportion_of_exp_used_for_predictor_update,
        policy=args.policy,
        int_coeff=args.int_coeff,
        ext_coeff=args.ext_coeff,
        dynamics_bonus=args.dynamics_bonus,
        cliprange=args.cliprange,
        nsteps=args.nsteps

    )
    logger.info("training parameters:")
    logger.info(str(hps))
    tf_util.make_session(make_default=True)
    train(origin_paper=origin_paper, env_id=args.env, num_env=args.num_env, seed=seed,
        num_timesteps=args.num_timesteps, hps=hps)


if __name__ == '__main__':
    from baselines.common.misc_util import boolean_flag

    parser = arg_parser()
    add_env_params(parser)
    parser.add_argument('--num-timesteps', type=int, default=int(1e6))
    parser.add_argument('--num_env', type=int, default=1)
    parser.add_argument('--use_news', type=int, default=0)
    parser.add_argument('--gamma', type=float, default=0.99)
    parser.add_argument('--gamma_ext', type=float, default=0.99)
    parser.add_argument('--lam', type=float, default=0.95)
    parser.add_argument('--update_ob_stats_every_step', type=int, default=0)
    parser.add_argument('--update_ob_stats_independently_per_gpu', type=int, default=0)
    parser.add_argument('--update_ob_stats_from_random_agent', type=int, default=1)
    parser.add_argument('--proportion_of_exp_used_for_predictor_update', type=float, default=1.)
    parser.add_argument('--tag', type=str, default='')
    parser.add_argument('--policy', type=str, default='cnn', choices=['cnn', 'rnn'])
    parser.add_argument('--int_coeff', type=float, default=1.)
    parser.add_argument('--ext_coeff', type=float, default=2.)
    parser.add_argument('--dynamics_bonus', type=int, default=0)
    parser.add_argument('--nminibatches', type=int, default=1)
    parser.add_argument('--nsteps', type=int, default=128)
    parser.add_argument('--max_grad_norm', type=float, default=0)
    parser.add_argument('--lr', type=float, default=0.0001)
    parser.add_argument('--ent_coef', type=float, default=0.001)
    parser.add_argument('--cliprange', type=float, default=0.1)
    boolean_flag(parser, 'origin_paper', default=False)
    boolean_flag(parser, 'server', default=False)
    boolean_flag(parser, 'all_env', default=False)
    # todo: change the following two parameters when you run your local machine or server

    args = parser.parse_args()

    list_p = []
    list_env = ["HalfCheetah-v2", "Hopper-v2", "Swimmer-v2", "InvertedDoublePendulum-v2", "Reacher-v2",
                "Walker2d-v2", "Pusher-v2"] if args.all_env else [args.env]
    for env in list_env:
        _args = deepcopy(args)
        _args.env = env
        p = Process(target=main, args=(_args, ))
        list_p.append((env, p))
        p.start()
        print("============================================")
        print("{} start!".format(env))
        print("============================================")
    for (env, p) in list_p:
        p.join()
        print("============================================")
        print("{} end!".format(env))
        print("============================================")
