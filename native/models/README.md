# models/

## deeptemp-k16-3.pb

TempoCNN tempo-estimation model (Schreiber & Müller, "A Single-Step Approach to Musical
Tempo Estimation Using a CNN", ISMIR 2018), as ported to TensorFlow by the Essentia
project. Loaded by `bpm.py` via `essentia.standard.TempoCNN`; expects 11025 Hz mono audio
and outputs an integer BPM in 30–286.

- Source: https://essentia.upf.edu/models/tempo/tempocnn/deeptemp-k16-3.pb
  (1,320,120 bytes, md5 2581282e7e08dedb7594ecff92217c8e)
- Model page: https://essentia.upf.edu/models.html#tempocnn
- Original: https://github.com/hendriks73/tempo-cnn

### License

The Essentia models are released under the **Creative Commons
Attribution-NonCommercial-ShareAlike 4.0 International (CC BY-NC-SA 4.0)** license:
https://creativecommons.org/licenses/by-nc-sa/4.0/

Attribution: "TempoCNN deeptemp-k16-3 model by the Music Technology Group, Universitat Pompeu
Fabra (Essentia), based on Schreiber & Müller 2018. Licensed CC BY-NC-SA 4.0." The model is
vendored here unmodified for personal, non-commercial use; this license applies to the
model file only, not to the rest of this repository (see ../LICENSE).
