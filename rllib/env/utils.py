import logging
from typing import List, Optional, Type, Union

import gymnasium as gym
import numpy as np
import tree  # pip install dm_tree

from ray.rllib.env.env_context import EnvContext
from ray.rllib.env.multi_agent_env import MultiAgentEnv
from ray.rllib.env.wrappers.multi_agent_env_compatibility import (
    MultiAgentEnvCompatibility,
)
from ray.rllib.utils.error import (
    ERR_MSG_INVALID_ENV_DESCRIPTOR,
    ERR_MSG_OLD_GYM_API,
    EnvError,
)
from ray.rllib.utils.gym import check_old_gym_env
from ray.rllib.utils.numpy import one_hot, one_hot_multidiscrete
from ray.rllib.utils.spaces.space_utils import (
    batch,
    get_dummy_batch_for_space,
    get_base_struct_from_space,
)
from ray.util import log_once
from ray.util.annotations import PublicAPI


logger = logging.getLogger(__name__)


@PublicAPI
def try_import_pyspiel(error: bool = False):
    """Tries importing pyspiel and returns the module (or None).

    Args:
        error: Whether to raise an error if pyspiel cannot be imported.

    Returns:
        The pyspiel module.

    Raises:
        ImportError: If error=True and pyspiel is not installed.
    """
    try:
        import pyspiel

        return pyspiel
    except ImportError:
        if error:
            raise ImportError(
                "Could not import pyspiel! Pygame is not a dependency of RLlib "
                "and RLlib requires you to install pygame separately: "
                "`pip install pygame`."
            )
        return None


@PublicAPI
def try_import_open_spiel(error: bool = False):
    """Tries importing open_spiel and returns the module (or None).

    Args:
        error: Whether to raise an error if open_spiel cannot be imported.

    Returns:
        The open_spiel module.

    Raises:
        ImportError: If error=True and open_spiel is not installed.
    """
    try:
        import open_spiel

        return open_spiel
    except ImportError:
        if error:
            raise ImportError(
                "Could not import open_spiel! open_spiel is not a dependency of RLlib "
                "and RLlib requires you to install open_spiel separately: "
                "`pip install open_spiel`."
            )
        return None


def _gym_env_creator(
    env_context: EnvContext,
    env_descriptor: Union[str, Type[gym.Env]],
    auto_wrap_old_gym_envs: bool = True,
) -> gym.Env:
    """Tries to create a gym env given an EnvContext object and descriptor.

    Note: This function tries to construct the env from a string descriptor
    only using possibly installed RL env packages (such as gym, pybullet_envs,
    etc). These packages are no installation requirements for RLlib. In case
    you would like to support more such env packages, add the necessary imports
    and construction logic below.

    Args:
        env_context: The env context object to configure the env.
            Note that this is a config dict, plus the properties:
            `worker_index`, `vector_index`, and `remote`.
        env_descriptor: The env descriptor as a gym-registered string, e.g. CartPole-v1,
            ALE/MsPacman-v5, or CartPoleContinuousBulletEnv-v0.
            Alternatively, the gym.Env subclass to use.
        auto_wrap_old_gym_envs: Whether to auto-wrap old gym environments (using
            the pre 0.24 gym APIs, e.g. reset() returning single obs and no info
            dict). If True, RLlib will automatically wrap the given gym env class
            with the gym-provided compatibility wrapper (gym.wrappers.EnvCompatibility).
            If False, RLlib will produce a descriptive error on which steps to perform
            to upgrade to gymnasium (or to switch this flag to True).

    Returns:
        The actual gym environment object.

    Raises:
        gym.error.Error: If the env cannot be constructed.
    """
    # Allow for PyBullet or envs to be used as well (via string). This allows
    # for doing things like `env=CartPoleContinuousBulletEnv-v0`.
    try:
        import pybullet_envs

        pybullet_envs.getList()
    except (AttributeError, ModuleNotFoundError, ImportError):
        pass

    # Try creating a gym env. If this fails we can output a
    # decent error message.
    try:
        # If class provided, call constructor directly.
        if isinstance(env_descriptor, type):
            env = env_descriptor(env_context)
        else:
            env = gym.make(env_descriptor, **env_context)
        # If we are dealing with an old gym-env API, use the provided compatibility
        # wrapper.
        if auto_wrap_old_gym_envs:
            try:
                # Call the env's reset() method to check for the env using the old
                # gym (reset doesn't take `seed` and `options` args and returns only
                # the initial observations) or new gymnasium APIs (reset takes `seed`
                # and `options` AND returns observations and infos).
                obs_and_infos = env.reset(seed=None, options={})
                # Check return values for correct gymnasium .
                check_old_gym_env(reset_results=obs_and_infos)
            # TypeError for `reset()` not accepting seed/options.
            # ValueError for `check_old_gym_env` raising error if return values
            # incorrect.
            except Exception:
                if log_once("auto_wrap_gym_api"):
                    logger.warning(
                        "`config.auto_wrap_old_gym_envs` is activated AND you seem to "
                        "have provided an old gym-API environment. RLlib will therefore"
                        " try to auto-fix the following error. However, please "
                        "consider switching over to the new `gymnasium` APIs:\n"
                        + ERR_MSG_OLD_GYM_API
                    )
                # Multi-agent case.
                if isinstance(env, MultiAgentEnv):
                    env = MultiAgentEnvCompatibility(env)
                # Single agent (gymnasium.Env) case.
                else:
                    env = gym.wrappers.EnvCompatibility(env)
                # Repeat the checks, now everything should work.
                obs_and_infos = env.reset(seed=None, options={})
                check_old_gym_env(reset_results=obs_and_infos)
    except gym.error.Error:
        raise EnvError(ERR_MSG_INVALID_ENV_DESCRIPTOR.format(env_descriptor))

    return env


class BufferWithInfiniteLookback:
    def __init__(
        self,
        data: Optional[Union[List, np.ndarray]] = None,
        lookback: int = 0,
        space: Optional[gym.Space] = None,
    ):
        self.data = data if data is not None else []
        self.lookback = lookback
        self.finalized = not isinstance(self.data, list)
        self.space = space
        self.space_struct = get_base_struct_from_space(self.space)

    def append(self, item) -> None:
        """Appends the given item to the end of this buffer."""
        if self.finalized:
            self.data = tree.map_structure(
                lambda d, i: np.concatenate([d, [i]], axis=0), self.data, item
            )
        else:
            self.data.append(item)

    def extend(self, items):
        """Appends all items in `items` to the end of this buffer."""
        if self.finalized:
            self.data = tree.map_structure(
                lambda d, i: np.concatenate([d, i], axis=0), self.data, np.array(items)
            )
        else:
            for item in items:
                self.append(item)

    def pop(self, index: int = -1) -> None:
        """Removes the item at `index` from this buffer, but does NOT return it.

        Args:
            index: The index to pop out of this buffer (w/o returning it from this
                method).
        """
        if self.finalized:
            self.data = tree.map_structure(
                lambda s: np.delete(s, index, axis=0), self.data
            )
        else:
            self.data.pop(index)

    def finalize(self):
        """Finalizes this buffer by converting internal data lists into numpy arrays.

        Thereby, if the individual items in the list are complex (nested 2)
        """
        if not self.finalized:
            self.data = batch(self.data)
            self.finalized = True

    def get(
        self,
        indices: Optional[Union[int, slice, List[int]]] = None,
        *,
        neg_indices_left_of_zero: bool = False,
        fill: Optional[float] = None,
        one_hot_discrete: bool = False,
    ):
        """Returns data, based on the given args, from this buffer.

        Args:
            indices: A single int is interpreted as an index, from which to return the
                individual data stored at this index.
                A list of ints is interpreted as a list of indices from which to gather
                individual data in a batch of size len(indices).
                A slice object is interpreted as a range of data to be returned.
                Thereby, negative indices by default are interpreted as "before the end"
                unless the `neg_indices_left_of_zero=True` option is used, in which case
                negative indices are interpreted as "before ts=0", meaning going back
                into the lookback buffer.
            neg_indices_left_of_zero: If True, negative values in `indices` are
                interpreted as "before ts=0", meaning going back into the lookback
                buffer. For example, an buffer with data [4, 5, 6,  7, 8, 9],
                where [4, 5, 6] is the lookback buffer range (ts=0 item is 7), will
                respond to `get(-1, neg_indices_left_of_zero=True)` with `6` and to
                `get(slice(-2, 1), neg_indices_left_of_zero=True)` with `[5, 6,  7]`.
            fill: An optional float value to use for filling up the returned results at
                the boundaries. This filling only happens if the requested index range's
                start/stop boundaries exceed the buffer's boundaries (including the
                lookback buffer on the left side). This comes in very handy, if users
                don't want to worry about reaching such boundaries and want to zero-pad.
                For example, a buffer with data [10, 11,  12, 13, 14] and lookback
                buffer size of 2 (meaning `10` and `11` are part of the lookback buffer)
                will respond to `get(slice(-7, -2), fill=0.0)`
                with `[0.0, 0.0, 10, 11, 12]`.
            one_hot_discrete: If True, will return one-hot vectors (instead of
                int-values) for those sub-components of a (possibly complex) space
                that are Discrete or MultiDiscrete. Note that if `fill=0` and the
                requested `indices` are out of the range of our data, the returned
                one-hot vectors will actually be zero-hot (all slots zero).
        """
        if fill is not None and self.space is None:
            raise ValueError(
                f"Cannot use `fill` argument in `{type(self).__name__}.get()` if a "
                "gym.Space was NOT provided during construction!"
            )

        if indices is None:
            data = self._get_all_data(one_hot_discrete=one_hot_discrete)
        elif isinstance(indices, slice):
            data = self._get_slice(
                indices,
                fill=fill,
                neg_indices_left_of_zero=neg_indices_left_of_zero,
                one_hot_discrete=one_hot_discrete,
            )
        elif isinstance(indices, list):
            data = [
                self._get_int_index(
                    idx,
                    fill=fill,
                    neg_indices_left_of_zero=neg_indices_left_of_zero,
                    one_hot_discrete=one_hot_discrete,
                )
                for idx in indices
            ]
            if self.finalized:
                data = batch(data)
        else:
            assert isinstance(indices, int)
            data = self._get_int_index(
                indices,
                fill=fill,
                neg_indices_left_of_zero=neg_indices_left_of_zero,
                one_hot_discrete=one_hot_discrete,
            )

        return data

    def __add__(self, other):
        """Adds another buffer or list to the end of this one.

        Args:
            other: Either `BufferWithInfiniteLookback` or `list`.
                If a `BufferWithInfiniteLookback` the data gets
                concatenated. If a `list` the list is concatenated to the
                `self.data`.

        Returns:
            A new `BufferWithInfiniteLookback` instance `self.data` cotnaining
            concatenated data from `self.` and `other`.
        """

        if self.finalized:
            raise RuntimeError(f"Cannot `add` to a finalized {type(self).__name__}.")
        else:
            if isinstance(other, BufferWithInfiniteLookback):
                data = self.data + other.data
            else:
                data = self.data + other
            return BufferWithInfiniteLookback(
                data=data,
                lookback=self.lookback,
                space=self.space,
            )

    def __getitem__(self, item):
        """Support squared bracket syntax, e.g. buffer[:5]."""
        return self.get(item)

    def __setitem__(self, key, value):
        self.set(new_data=value, at_indices=key)

    def set(
        self,
        new_data,
        *,
        at_indices: Optional[Union[int, slice, List[int]]] = None,
        neg_indices_left_of_zero: bool = False,
    ) -> None:
        """Overwrites all or some of the data in this buffer with the provided data.

        Args:
            new_data: The new data to overwrite existing records with.
            at_indices: A single int is interpreted as an index, at which to overwrite
                the individual record stored at this index with `new_data`.
                A list of ints is interpreted as a list of indices, which to overwrite
                with `new_data`, which must be a batch of size `len(at_indices)`.
                A slice object is interpreted as a range, which to overwrite with
                `new_data`. Thereby, negative indices by default are interpreted as
                "before the end" unless the `neg_indices_left_of_zero=True` option is
                used, in which case negative indices are interpreted as
                "before ts=0", meaning going back into the lookback buffer.
            neg_indices_left_of_zero: If True, negative values in `at_indices` are
                interpreted as "before ts=0", meaning going back into the lookback
                buffer. For example, a buffer with data [4, 5, 6,  7, 8, 9],
                where [4, 5, 6] is the lookback buffer range (ts=0 item is 7), will
                handle a call `set(99, at_indices=-1, neg_indices_left_of_zero=True)`
                with `6` being replaced by 99 and to `set([98, 99, 100],
                at_indices=slice(-2, 1), neg_indices_left_of_zero=True)` with
                `[5, 6,  7]` being replaced by `[98, 99,  100]`.
        """
        # `at_indices` is None -> Override all our data (excluding the lookback buffer).
        if at_indices is None:
            self._set_all_data(new_data)

        elif isinstance(at_indices, slice):
            self._set_slice(
                new_data,
                slice_=at_indices,
                neg_indices_left_of_zero=neg_indices_left_of_zero,
            )
        elif isinstance(at_indices, list):
            for i, idx in enumerate(at_indices):
                self._set_int_index(
                    new_data[i],
                    idx=idx,
                    neg_indices_left_of_zero=neg_indices_left_of_zero,
                )
        else:
            assert isinstance(at_indices, int)
            self._set_int_index(
                new_data,
                idx=at_indices,
                neg_indices_left_of_zero=neg_indices_left_of_zero,
            )

    def __len__(self):
        """Return the length of our data, excluding the lookback buffer."""
        if self.finalized:
            len_ = len(tree.flatten(self.data)[0])
        else:
            len_ = len(self.data)
        # Only count the data after the lookback.
        return max(len_ - self.lookback, 0)

    def _get_all_data(self, one_hot_discrete=False):
        data = self[:]
        if one_hot_discrete:
            data = self._one_hot(data, space_struct=self.space_struct)
        return data

    def _set_all_data(self, new_data):
        self._set_slice(new_data, slice(0, None))

    def _get_slice(
        self,
        slice_,
        fill=None,
        neg_indices_left_of_zero=False,
        one_hot_discrete=False,
    ):
        slice_, slice_len, fill_left_count, fill_right_count = self._interpret_slice(
            slice_, neg_indices_left_of_zero
        )

        # Perform the actual slice.
        if self.finalized:
            data_slice = tree.map_structure(lambda s: s[slice_], self.data)
        else:
            data_slice = self.data[slice_]

        if one_hot_discrete and slice_len > 0:
            data_slice = self._one_hot(data_slice, space_struct=self.space_struct)

        # Data is shorter than the range requested -> Fill the rest with `fill` data.
        if fill is not None and (fill_right_count > 0 or fill_left_count > 0):
            if self.finalized:
                if fill_left_count:
                    fill_batch = get_dummy_batch_for_space(
                        self.space,
                        fill_value=fill,
                        batch_size=fill_left_count,
                        one_hot_discrete=one_hot_discrete,
                    )
                    if data_slice is not None:
                        data_slice = tree.map_structure(
                            lambda s0, s: np.concatenate([s0, s]),
                            fill_batch,
                            data_slice,
                        )
                    else:
                        data_slice = fill_batch
                if fill_right_count:
                    fill_batch = get_dummy_batch_for_space(
                        self.space,
                        fill_value=fill,
                        batch_size=fill_right_count,
                        one_hot_discrete=one_hot_discrete,
                    )
                    if data_slice is not None:
                        data_slice = tree.map_structure(
                            lambda s0, s: np.concatenate([s, s0]),
                            fill_batch,
                            data_slice,
                        )
                    else:
                        data_slice = fill_batch

            else:
                fill_batch = [
                    get_dummy_batch_for_space(
                        self.space,
                        fill_value=fill,
                        batch_size=0,
                        one_hot_discrete=one_hot_discrete,
                    )
                ]
                data_slice = (
                    fill_batch * fill_left_count
                    + (data_slice if data_slice is not None else [])
                    + fill_batch * fill_right_count
                )

        return data_slice

    def _set_slice(
        self,
        new_data,
        slice_,
        neg_indices_left_of_zero=False,
    ):
        slice_, _, _, _ = self._interpret_slice(slice_, neg_indices_left_of_zero)

        # Check, whether the setting to new_data changes the length of self
        # (it shouldn't). If it does, raise an error.
        try:
            if self.finalized:

                def __set(s, n):
                    if self.space:
                        assert self.space.contains(n[0])
                    assert len(s[slice_]) == len(n)
                    s[slice_] = n

                tree.map_structure(__set, self.data, new_data)
            else:
                if self.space:
                    assert self.space.contains(new_data[0])
                assert len(self.data[slice_]) == len(new_data)
                self.data[slice_] = new_data
        except AssertionError:
            raise IndexError(
                f"Cannot `set()` value via at_indices={slice_} (option "
                f"neg_indices_left_of_zero={neg_indices_left_of_zero})! Slice of data "
                "does NOT have the same size as `new_data`."
            )

    def _get_int_index(
        self,
        idx: int,
        fill=None,
        neg_indices_left_of_zero=False,
        one_hot_discrete=False,
    ):
        # If index >= 0 -> Ignore lookback buffer.
        # Otherwise, include lookback buffer.
        if idx >= 0 or neg_indices_left_of_zero:
            idx = self.lookback + idx
        # Negative indices mean: Go to left into lookback buffer starting from idx=0.
        # But if we pass the lookback buffer, the index should be invalid and we will
        # have to fill, if required. Invalidate the index by setting it to one larger
        # than max.
        if neg_indices_left_of_zero and idx < 0:
            idx = len(self) + self.lookback

        try:
            if self.finalized:
                data = tree.map_structure(lambda s: s[idx], self.data)
            else:
                data = self.data[idx]
        # Out of range index -> If `fill`, use a fill dummy (B=0), if not, error out.
        except IndexError as e:
            if fill is not None:
                return get_dummy_batch_for_space(
                    self.space,
                    fill_value=fill,
                    batch_size=0,
                    one_hot_discrete=one_hot_discrete,
                )
            else:
                raise e

        # Convert discrete/multi-discrete components to one-hot vectors, if required.
        if one_hot_discrete:
            data = self._one_hot(data, self.space_struct)
        return data

    def _set_int_index(self, new_data, idx, neg_indices_left_of_zero):
        actual_idx = idx
        # If index >= 0 -> Ignore lookback buffer.
        # Otherwise, include lookback buffer.
        if actual_idx >= 0 or neg_indices_left_of_zero:
            actual_idx = self.lookback + actual_idx
        # Negative indices mean: Go to left into lookback buffer starting from idx=0.
        # But if we pass the lookback buffer, the index should be invalid and we will
        # have to fill, if required. Invalidate the index by setting it to one larger
        # than max.
        if neg_indices_left_of_zero and actual_idx < 0:
            actual_idx = len(self) + self.lookback

        try:
            if self.finalized:

                def __set(s, n):
                    if self.space:
                        assert self.space.contains(n), n
                    s[actual_idx] = n

                tree.map_structure(__set, self.data, new_data)
            else:
                if self.space:
                    assert self.space.contains(new_data), new_data
                self.data[actual_idx] = new_data
        except IndexError:
            raise IndexError(
                f"Cannot `set()` value at index {idx} (option "
                f"neg_indices_left_of_zero={neg_indices_left_of_zero})! Out of range "
                f"of buffer data."
            )

    def _interpret_slice(self, slice_, neg_indices_left_of_zero):
        len_self_plus_lookback = len(self) + self.lookback
        fill_left_count = fill_right_count = 0

        # Re-interpret slice bounds as absolute positions (>=0) within our
        # internal data.
        start = slice_.start
        stop = slice_.stop

        # Start is None -> Exclude lookback buffer.
        if start is None:
            start = self.lookback
        # Start is negative.
        elif start < 0:
            # `neg_indices_left_of_zero=True` -> User wants to index into the lookback
            # range.
            if neg_indices_left_of_zero:
                start = self.lookback + start
            # Interpret index as counting "from end".
            else:
                start = len_self_plus_lookback + start
        # Start is 0 or positive -> timestep right after lookback is interpreted as 0.
        else:
            start = self.lookback + start

        # Stop is None -> Set stop to very last index + 1 of our internal data.
        if stop is None:
            stop = len_self_plus_lookback
        # Stop is negative.
        elif stop < 0:
            # `neg_indices_left_of_zero=True` -> User wants to index into the lookback
            # range. Set to 0 (beginning of lookback buffer) if result is a negative
            # index.
            if neg_indices_left_of_zero:
                stop = self.lookback + stop
            # Interpret index as counting "from end". Set to 0 (beginning of actual
            # episode) if result is a negative index.
            else:
                stop = len_self_plus_lookback + stop
        # Stop is positive -> Add lookback range to it.
        else:
            stop = self.lookback + stop

        # Both start and stop are on left side.
        if start < 0 and stop < 0:
            fill_left_count = abs(start - stop)
            fill_right_count = 0
            start = stop = 0
        # Both start and stop are on right side.
        elif start >= len_self_plus_lookback and stop >= len_self_plus_lookback:
            fill_right_count = abs(start - stop)
            fill_left_count = 0
            start = stop = len_self_plus_lookback
        # Set to 0 (beginning of actual episode) if result is a negative index.
        elif start < 0:
            fill_left_count = -start
            start = 0
        elif stop >= len_self_plus_lookback:
            fill_right_count = stop - len_self_plus_lookback
            stop = len_self_plus_lookback

        assert start >= 0 and stop >= 0, (start, stop)
        assert start <= len_self_plus_lookback and stop <= len_self_plus_lookback, (
            start,
            stop,
        )

        step = slice_.step if slice_.step is not None else 1
        slice_ = slice(start, stop, step)
        slice_len = max(0, (stop - start + (step - (1 if step > 0 else -1))) // step)
        return slice_, slice_len, fill_left_count, fill_right_count

    def _one_hot(self, data, space_struct):
        if space_struct is None:
            raise ValueError(
                f"Cannot `one_hot` data in `{type(self).__name__}` if a "
                "gym.Space was NOT provided during construction!"
            )

        def _convert(dat_, space):
            if isinstance(space, gym.spaces.Discrete):
                return one_hot(dat_, depth=space.n)
            elif isinstance(space, gym.spaces.MultiDiscrete):
                return one_hot_multidiscrete(dat_, depths=space.nvec)
            return dat_

        if isinstance(data, list):
            data = [
                tree.map_structure(_convert, dslice, space_struct) for dslice in data
            ]
        else:
            data = tree.map_structure(_convert, data, space_struct)
        return data
