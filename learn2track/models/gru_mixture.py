import numpy as np
import theano
import theano.tensor as T
from collections import OrderedDict
from smartlearner.interfaces import Loss
from theano.sandbox.rng_mrg import MRG_RandomStreams

from learn2track.models.gru_regression import GRU_Regression
from learn2track.models.layers import LayerRegression
from learn2track.utils import logsumexp, softmax, l2distance

floatX = theano.config.floatX


class GRU_Mixture(GRU_Regression):
    """ A GRU_Regression model with the output size computed for a mixture of gaussians, using a diagonal covariance matrix
    """

    def __init__(self, volume_manager, input_size, hidden_sizes, output_size, n_gaussians, use_previous_direction=False, **_):
        """
        Parameters
        ----------
        volume_manager : :class:`VolumeManger` object
            Use to evaluate the diffusion signal at specific coordinates.
        input_size : int
            Number of units each element Xi in the input sequence X has.
        hidden_sizes : int, list of int
            Number of hidden units each GRU should have.
        output_size : int
            Number of units the regression layer should have.
        n_gaussians : int
            Number of gaussians in the mixture
        use_previous_direction : bool
            Use the previous direction as an additional input
        """
        super(GRU_Regression, self).__init__(input_size, hidden_sizes)
        self.volume_manager = volume_manager
        self.n_gaussians = n_gaussians

        assert output_size == 3  # Only 3-dimensional target is supported for now
        self.output_size = output_size

        self.use_previous_direction = use_previous_direction

        # GRU_Mixture does not predict a direction, so it cannot predict an offset
        self.predict_offset = False

        self.layer_regression_size = sum([n_gaussians,  # Mixture weights
                                          n_gaussians * output_size,  # Means
                                          n_gaussians * output_size])  # Stds
        self.layer_regression = LayerRegression(self.hidden_sizes[-1], self.layer_regression_size)

    @property
    def hyperparameters(self):
        hyperparameters = super().hyperparameters
        hyperparameters['n_gaussians'] = self.n_gaussians
        hyperparameters['layer_regression_size'] = self.layer_regression_size
        hyperparameters['use_previous_direction'] = self.use_previous_direction
        return hyperparameters

    def get_mixture_parameters(self, regression_output, ndim=3):
        shape_split_by_gaussian = T.concatenate([regression_output.shape[:-1], [self.n_gaussians, 3]])
        mixture_weights = softmax(regression_output[..., :self.n_gaussians], axis=-1)
        means = T.reshape(regression_output[..., self.n_gaussians:self.n_gaussians * 4], shape_split_by_gaussian, ndim=ndim)
        stds = T.reshape(T.exp(regression_output[..., self.n_gaussians * 4:self.n_gaussians * 7]), shape_split_by_gaussian, ndim=ndim)

        return mixture_weights, means, stds

    def _get_stochastic_samples(self, srng, mixture_weights, means, stds):
        batch_size = mixture_weights.shape[0]
        xs = T.arange(0, batch_size)

        choices = T.argmax(srng.multinomial(n=1, pvals=mixture_weights), axis=1)

        # means[0] : [[mean_x1, mean_y1, mean_z1], ..., [mean_xn, mean_yn, mean_zn]]

        # mu.shape : (batch_size, 3)
        mu = means[xs, choices]

        # sigma.shape : (batch_size, 3)
        sigma = stds[xs, choices]

        noise = srng.normal((batch_size, 3))
        samples = mu + sigma * noise

        return samples

    def _get_max_component_samples(self, mixture_weights, means, stds):
        batch_size = mixture_weights.shape[0]
        xs = T.arange(0, batch_size)

        choices = T.argmax(mixture_weights, axis=1)
        samples = means[xs, choices]

        return samples

    def make_sequence_generator(self, subject_id=0, use_max_component=False):
        """ Makes functions that return the prediction for x_{t+1} for every
        sequence in the batch given x_{t} and the current state of the model h^{l}_{t}.

        Parameters
        ----------
        subject_id : int, optional
            ID of the subject from which its diffusion data will be used. Default: 0.
        """

        # Build the sequence generator as a theano function.
        states_h = []
        for i in range(len(self.hidden_sizes)):
            state_h = T.matrix(name="layer{}_state_h".format(i))
            states_h.append(state_h)

        symb_x_t = T.matrix(name="x_t")

        new_states = self._fprop_step(symb_x_t, *states_h)
        new_states_h = new_states[:len(self.hidden_sizes)]

        # regression_output.shape : (batch_size, target_size)
        regression_output = new_states[-1]
        mixture_params = self.get_mixture_parameters(regression_output, ndim=3)

        if use_max_component:
            predictions = self._get_max_component_samples(*mixture_params)
        else:
            srng = MRG_RandomStreams(1234)
            predictions = self._get_stochastic_samples(srng, *mixture_params)

        f = theano.function(inputs=[symb_x_t] + states_h,
                            outputs=[predictions] + list(new_states_h))

        def _gen(x_t, states, previous_direction=None):
            """ Returns the prediction for x_{t+1} for every
                sequence in the batch given x_{t} and the current states
                of the model h^{l}_{t}.

            Parameters
            ----------
            x_t : ndarray with shape (batch_size, 3)
                Streamline coordinate (x, y, z).
            states : list of 2D array of shape (batch_size, hidden_size)
                Currrent states of the network.
            previous_direction : ndarray with shape (batch_size, 3)
                If using previous direction, these should be added to the input

            Returns
            -------
            next_x_t : ndarray with shape (batch_size, 3)
                Directions to follow.
            new_states : list of 2D array of shape (batch_size, hidden_size)
                Updated states of the network after seeing x_t.
            """
            # Append the DWI ID of each sequence after the 3D coordinates.
            subject_ids = np.array([subject_id] * len(x_t), dtype=floatX)[:, None]

            if not self.use_previous_direction:
                x_t = np.c_[x_t, subject_ids]
            else:
                x_t = np.c_[x_t, subject_ids, previous_direction]

            results = f(x_t, *states)
            next_x_t = results[0]
            new_states = results[1:]
            return next_x_t, new_states

        return _gen


class MultivariateGaussianMixtureNLL(Loss):
    """ Computes the likelihood of a multivariate gaussian mixture
    """

    def __init__(self, model, dataset, sum_over_timestep=False, consider_only_top_10=False):
        super().__init__(model, dataset)
        self.n = model.n_gaussians
        self.d = model.output_size
        self.sum_over_timestep = sum_over_timestep
        self.consider_only_top_10 = consider_only_top_10

    def _get_updates(self):
        return {}  # There is no updates for L2Distance.

    def _compute_losses(self, model_output):
        mask = self.dataset.symb_mask

        # regression_outputs.shape = (batch_size, seq_length, regression_layer_size)
        regression_outputs = model_output

        mixture_weights, means, stds = self.model.get_mixture_parameters(regression_outputs, ndim=4)

        # means.shape : (batch_size, seq_len, n_gaussians, 3)

        # mean_*.shape : (batch_size, seq_len, n_gaussians)
        mean_x = means[:, :, :, 0]
        mean_y = means[:, :, :, 1]
        mean_z = means[:, :, :, 2]

        # std_*.shape : (batch_size, seq_len, n_gaussians)
        std_x = stds[:, :, :, 0]
        std_y = stds[:, :, :, 1]
        std_z = stds[:, :, :, 2]

        # target_*.shape : (batch_size, seq_len, 1)
        target_x = self.dataset.symb_targets[:, :, 0, None]
        target_y = self.dataset.symb_targets[:, :, 1, None]
        target_z = self.dataset.symb_targets[:, :, 2, None]

        tg_x_c = (target_x - mean_x) / std_x
        tg_y_c = (target_y - mean_y) / std_y
        tg_z_c = (target_z - mean_z) / std_z

        log_prefix = T.log(mixture_weights) - np.float32((self.d / 2.) * np.log(2 * np.pi)) - T.log(std_x) - T.log(std_y) - T.log(std_z)
        square_mahalanobis_dist = -0.5 * (tg_x_c ** 2 + tg_y_c ** 2 + tg_z_c ** 2)

        # loss_per_timestep.shape : (batch_size, seq_len)
        self.loss_per_time_step = - logsumexp(log_prefix + square_mahalanobis_dist, axis=2)

        # loss_per_seq.shape : (batch_size,)
        self.loss_per_seq = T.sum(self.loss_per_time_step * mask, axis=1)

        if not self.sum_over_timestep:
            self.loss_per_seq /= T.sum(mask, axis=1)
        elif self.consider_only_top_10:
            n = 10
            masked_loss_per_time_step = self.loss_per_time_step * mask
            self.loss_per_seq = T.sum(
                masked_loss_per_time_step[
                    T.arange(mixture_weights.shape[0])[:, None],
                    T.argsort(masked_loss_per_time_step, axis=1)[:, -n:]],
                axis=1)

        return self.loss_per_seq


class MultivariateGaussianMixtureExpectedValueL2Distance(Loss):
    """ Computes the L2 distance for the expected value samples of a multivariate gaussian mixture
    """

    def __init__(self, model, dataset):
        super().__init__(model, dataset)
        self.n = model.n_gaussians
        self.d = model.output_size

    def _get_updates(self):
        return {}  # There is no updates for L2Distance.

    def _compute_losses(self, model_output):
        mask = self.dataset.symb_mask

        # regression_outputs.shape = (batch_size, seq_length, regression_layer_size)
        regression_outputs = model_output

        mixture_weights, means, stds = self.model.get_mixture_parameters(regression_outputs, ndim=4)

        # mixture_weights.shape : (batch_size, seq_len, n_gaussians)
        # means.shape : (batch_size, seq_len, n_gaussians, 3)

        # samples.shape : (batch_size, seq_len, 3)
        self.samples = T.sum(mixture_weights[:, :, :, None] * means, axis=2)

        # loss_per_time_step.shape = (batch_size, seq_len)
        self.loss_per_time_step = l2distance(self.samples, self.dataset.symb_targets)
        # loss_per_seq.shape = (batch_size,)
        self.loss_per_seq = T.sum(self.loss_per_time_step*mask, axis=1) / T.sum(mask, axis=1)

        return self.loss_per_seq


class MultivariateGaussianMixtureMaxComponentL2Distance(Loss):
    """ Computes the L2 distance for the max component samples of a multivariate gaussian mixture
    """

    def __init__(self, model, dataset):
        super().__init__(model, dataset)
        self.n = model.n_gaussians
        self.d = model.output_size

    def _get_updates(self):
        return {}  # There is no updates for L2Distance.

    def _compute_losses(self, model_output):
        mask = self.dataset.symb_mask

        # regression_outputs.shape = (batch_size, seq_length, regression_layer_size)
        regression_outputs = model_output

        # mixture_weights.shape : (batch_size, seq_len, n_gaussians)
        # means.shape : (batch_size, seq_len, n_gaussians, 3)
        mixture_weights, means, stds = self.model.get_mixture_parameters(regression_outputs, ndim=4)
        maximum_component_ids = T.argmax(mixture_weights, axis=2)

        # samples.shape : (batch_size, seq_len, 3)
        self.samples = means[(T.arange(mixture_weights.shape[0])[:, None]),
                             (T.arange(mixture_weights.shape[1])[None, :]),
                             maximum_component_ids]

        # loss_per_time_step.shape = (batch_size, seq_len)
        self.loss_per_time_step = l2distance(self.samples, self.dataset.symb_targets)
        # loss_per_seq.shape = (batch_size,)
        self.loss_per_seq = T.sum(self.loss_per_time_step*mask, axis=1) / T.sum(mask, axis=1)

        return self.loss_per_seq
