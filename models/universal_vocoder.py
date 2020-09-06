"""Universal vocoder"""
import torch
import torch.nn as nn
import torch.nn.functional as F


class UniversalVocoder(nn.Module):
    """Universal vocoding"""

    def __init__(
        self,
        sample_rate,
        frames_per_sample,
        frames_per_slice,
        mel_dim,
        mel_rnn_dim,
        emb_dim,
        wav_rnn_dim,
        affine_dim,
        bits,
        hop_length,
    ):
        super().__init__()

        self.sample_rate = sample_rate
        self.frames_per_slice = frames_per_slice
        self.pad = (frames_per_sample - frames_per_slice) // 2
        self.wav_rnn_dim = wav_rnn_dim
        self.quant_dim = 2 ** bits
        self.hop_len = hop_length

        self.mel_rnn = nn.GRU(
            mel_dim, mel_rnn_dim, num_layers=2, batch_first=True, bidirectional=True
        )
        self.embedding = nn.Embedding(self.quant_dim, emb_dim)
        self.wav_rnn = nn.GRU(emb_dim + 2 * mel_rnn_dim, wav_rnn_dim, batch_first=True)
        self.affine = nn.Sequential(
            nn.Linear(wav_rnn_dim, affine_dim),
            nn.ReLU(),
            nn.Linear(affine_dim, self.quant_dim),
        )

    def forward(self, wavs, mels):
        """Generate waveform from mel spectrogram with teacher-forcing."""
        mel_embs, _ = self.mel_rnn(mels)
        mel_embs = mel_embs.transpose(1, 2)
        mel_embs = mel_embs[:, :, self.pad : self.pad + self.frames_per_slice]

        conditions = F.interpolate(mel_embs, scale_factor=float(self.hop_len))
        conditions = conditions.transpose(1, 2)

        wav_embs = self.embedding(wavs)
        wav_outs, _ = self.wav_rnn(torch.cat((wav_embs, conditions), dim=2))

        return self.affine(wav_outs)

    @torch.jit.export
    def generate(self, mels):
        """Generate waveform from mel spectrogram."""
        mel_embs, _ = self.mel_rnn(mels)
        mel_embs = mel_embs.transpose(1, 2)

        conditions = F.interpolate(mel_embs, scale_factor=float(self.hop_len))
        conditions = conditions.transpose(1, 2)

        hid = torch.zeros(mels.size(0), 1, self.wav_rnn_dim, device=mels.device)
        wav = torch.full(
            (mels.size(0),), self.quant_dim // 2, dtype=torch.long, device=mels.device,
        )
        wavs = torch.empty(
            mels.size(0), mels.size(1) * self.hop_len, device=mels.device
        )

        for i, condition in enumerate(torch.unbind(conditions, dim=1)):
            wav_emb = self.embedding(wav)
            _, hid = self.wav_rnn(
                torch.cat((wav_emb, condition), dim=1).unsqueeze(1), hid
            )
            logit = self.affine(hid.squeeze(1))
            posterior = F.softmax(logit, dim=1)
            wav = torch.multinomial(posterior, 1).squeeze(1)
            wavs[:, i] = 2 * wav.item() / (self.quant_dim - 1.0) - 1.0

        mu = self.quant_dim - 1
        wavs = torch.sign(wavs) / mu * ((1 + mu) ** torch.abs(wavs) - 1)

        return wavs