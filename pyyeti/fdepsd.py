# -*- coding: utf-8 -*-
"""
Tools for calculating the fatigue damage equivalent PSD. Adapted and
enhanced from the CAM versions.
"""

import numpy as np
import scipy.signal as signal
from pyyeti import cyclecount
from pyyeti import srs
from types import SimpleNamespace
import itertools as it
import multiprocessing as mp


def _to_np_array(sh_arr):
    return np.frombuffer(sh_arr[0]).reshape(sh_arr[1])


def _mk_par_globals(wn, sig, avs, binamps, count):
    global WN, SIG, ASV, BinAmps, Count
    WN = _to_np_array(wn)
    SIG = _to_np_array(sig)
    ASV = _to_np_array(avs)
    BinAmps = _to_np_array(binamps)
    Count = _to_np_array(count)


def _dofde(args):
    """Utility routine for parallel processing"""
    (j, (coeffunc, Q, dT, verbose)) = args
    if verbose:
        print('Processing frequency {:8.2f} Hz'.
              format(WN[j]/2/np.pi), end='\r')
    b, a = coeffunc(Q, dT, WN[j])
    resphist = signal.lfilter(b, a, SIG)
    ASV[1, j] = abs(resphist).max()
    ASV[2, j] = np.var(resphist)

    # use rainflow to count cycles:
    ind = cyclecount.findap(resphist)
    rf = cyclecount.rainflow(resphist[ind])

    amp = rf['amp']
    count = rf['count']
    ASV[0, j] = amp.max()
    BinAmps[j] *= ASV[0, j]

    # cumulative bin count:
    for jj in range(BinAmps.shape[1]):
        pv = amp >= BinAmps[j, jj]
        Count[j, jj] = np.sum(count[pv])


def fdepsd(sig, sr, freq, Q, resp='absacce', hpfilter=5., nbins=300,
           T0=60., rolloff='lanczos', ppc=12, parallel='auto',
           maxcpu=14, verbose=False):
    r"""
    Compute a fatigue damage equivalent PSD from a signal.

    Parameters
    ----------
    sig : 1d array_like
        Base acceleration signal.
    sr : scalar
        Sample rate.
    freq : array_like
        Frequency vector in Hz. This defines the single DOF (SDOF)
        systems to use.
    Q : scalar > 0.5
        Dynamic amplification factor :math:`Q = 1/(2\zeta)` where
        :math:`\zeta` is the fraction of critical damping.
    resp : string; optional
        The type of response to base the damage calculations on:

        =========    =======================================
         `resp`      Damage is based on
        =========    =======================================
        'absacce'    absolute acceleration [#fde1]_
        'pvelo'      pseudo velocity [#fde2]_
        =========    =======================================

    hpfilter : scalar or None; optional
        High pass filter frequency; if None, no filtering is done.
        If filtering is done, it is a 3rd order butterworth via
        :func:`scipy.signal.filtfilt`.
    nbins : integer; optional
        The number of amplitude levels at which to count cycles
    T0 : scalar; optional
        Specifies test duration in seconds
    rolloff : string or function or None; optional
        Indicate which method to use to account for the SRS roll off
        when the minimum `ppc` value is not met. Either 'fft' or
        'lanczos' seem the best.  If a string, it must be one of these
        values:

        ===========    ==========================================
        `rolloff`      Notes
        ===========    ==========================================
        'fft'          Use FFT to upsample data as needed.  See
                       :func:`scipy.signal.resample`.
        'lanczos'      Use Lanczos resampling to upsample as
                       needed. See :func:`ytools.resample`.
        'prefilter'    Apply a high freq. gain filter to account
                       for the SRS roll-off. See
                       :func:`srs.preroll` for more information.
                       This option ignores `ppc`.
        'linear'       Use linear interpolation to increase the
                       points per cycle (this is not recommended;
                       method; it's only here as a test case).
        'none'         Don't do anything to enforce the minimum
                       `ppc`. Note error bounds listed above.
         None          Same as 'none'.
        ===========    ==========================================

        If a function, the call signature is:
        ``sig_new, sr_new = rollfunc(sig, sr, ppc, frq)``. Here, `sig`
        is 1 x time. The last three inputs are scalars. For example,
        the 'fft' function is (trimmed of documentation)::

            def fftroll(sig, sr, ppc, frq):
                N = np.size(sig, 1)
                if N > 1:
                    curppc = sr/frq
                    factor = np.ceil(ppc/curppc)
                    sig = signal.resample(sig, int(factor)*N, axis=1)
                    sr *= factor
                return sig, sr

    ppc : scalar; optional
        Specifies the minimum points per cycle for SRS calculations.
        See also `rolloff`.

        ======    ==================================
        `ppc`     Maximum error at highest frequency
        ======    ==================================
            3     81.61%
            4     48.23%
            5     31.58%
           10     8.14% (minimum recommended `ppc`)
           12     5.67%
           15     3.64%
           20     2.05%
           25     1.31%
           50     0.33%
        ======    ==================================

    parallel : string; optional
        Controls the parallelization of the calculations:

        ==========   ============================================
        `parallel`   Notes
        ==========   ============================================
        'auto'       Routine determines whether or not to run
                     parallel.
        'no'         Do not use parallel processing.
        'yes'        Use parallel processing. Beware, depending
                     on the particular problem, using parallel
                     processing can be slower than not using it.
        ==========   ============================================

    maxcpu : integer or None; optional
        Specifies maximum number of CPUs to use. If None, it is
        internally set to 4/5 of available CPUs (as determined from
        :func:`multiprocessing.cpu_count`.
    verbose : bool; optional
        If True, routine will print some status information.

    Returns
    -------
    A record (SimpleNamespace class) with the members:

    freq : 1d ndarray
        Same as input `freq`.
    psd : 2d ndarray; ``len(freq) x 5``
        The five columns are: [G1, G2, G4, G8, G12]:

        ===========   ===============================================
           Name       Description
        ===========   ===============================================
            G1        The "G1" PSD (Mile's or similar equivalent from
                      SRS); uses the maximum cycle amplitude instead
                      of the raw SRS peak for each frequency. G1 is
                      not a damage-based PSD.
            G2        The "G2" PSD of reference [#fde1]_; G2 >= G1 by
                      bounding lower amplitude counts down to 1/3 of
                      the maximum cycle amplitude. G2 is not a
                      damage-based PSD.
        G4, G8, G12   The damage-based PSDs with fatigue exponents of
                      4, 8, and 12
        ===========   ===============================================

    amp : 2d ndarray; ``len(freq) x 5``
        The five columns correspond to the columns in `psd`. They are
        the Mile's equation (or similar if using ``resp='pvelo'``)
        SDOF (single DOF oscillator) peak response using the peak
        factor ``sqrt(2*log(f*T0))``. Note that the first column is,
        by definition, the maximum cycle amplitude for each SDOF from
        the rainflow count (G1 was calculated from this). Typically,
        this should be very close to the raw SRS peaks but a little
        lower since SRS just grabs peaks without consideration of the
        opposite peak.
    binamps : 2d ndarray; ``len(freq) x nbins``
        Each row (for a specific frequency SDOF) in `binamps` contains
        the lower amplitude boundary of each bin. Spacing of the bins
        is linear. The next column for this matrix would be
        ``amp[:, 0]``.
    count : 2d ndarray; ``len(freq) x nbins``
        Summary matrix of the rainflow cycle counts. Size corresponds
        with `binamps` and the count is cumulative; that is, the count
        in each entry includes cycles at the `binamps` amplitude and
        above. Therefore, first column has total cycles for the SDOF.
    srs : 1d ndarray; length = ``len(freq)``
        The raw SRS peaks version of the first column in `amp`. See
        `amp`.
    var : 1d ndarray; length = ``len(freq)``
        Vector of the SDOF response variances.
    parallel : string
        Either 'yes' or 'no' depending on whether parallel processing
        was used or not.
    ncpu : integer
        Specifies the number of CPUs used.
    resp : string
        Same as the input `resp`.

    Notes
    -----
    Steps (see [#fde1]_, [#fde2]_):
      1.  Resample signal to higher rate if highest frequency would
          have less than `ppc` points-per-cycle. Method of increasing
          the sample rate is controlled by the `rolloff` input.
      2.  For each frequency:

          a.  Compute the SDOF base-drive response
          b.  Calculate `srs` and `var` outputs
          c.  Use :func:`cyclecount.findap` to find cycle peaks
          d.  Use :func:`cyclecount.rainflow` to count cycles and
              amplitudes
          e.  Put counts into amplitude bins

      3.  Calculate `g1` based on cycle amplitudes from maximum
          amplitude (step 2d) and Mile's (or similar) equation.
      4.  Calculate `g2` to bound `g1` & lower amplitude cycles with
          high counts.  Ignore amplitudes < ``Amax/3``.
      5.  Calculate damage indicators from data with b = 4, 8, 12
          where b is the fatigue exponent.
      6.  Calculate damage indicators for `T0` second test with
          b = 4, 8, 12.
      7.  Solve for `g4`, `g8`, `g12` from results of steps 5 & 6.

    No checks are done regarding the suitability of this method for
    the input data. It is recommended to read the references [#fde1]_
    [#fde2]_ and do those checks (such as plotting Count or Time
    vs. Amp**2 and comparing to theoretical).

    The Mile's equation (or similar) is used in this methodology to
    relate acceleration PSDs to peak responses. If `resp` is
    'absacce', it is the Mile's equation:

    .. math::
        \sigma_{absacce}(f) = \sqrt{\frac{\pi}{2} \cdot f \cdot Q
        \cdot PSD(f)}

    If `resp` is 'pvelo', the similar equation is:

    .. math::
        \sigma_{pvelo}(f) = \sqrt{\frac{Q \cdot  PSD(f)}{8 \pi f}}

    Those two equations assume a flat acceleration PSD. Therefore, it
    is recommended to compare SDOF responses from flight data (SRS) to
    SDOF VRS responses from the developed specification (see
    :func:`srs.vrs` to compute the VRS response in the
    absolute-acceleration case). This is to check for conservatism.
    Instead of using 3 for peak factor (for 3-rms or 3-sigma) , use
    ``sqrt(2*log(f*T0))`` for the peak factor. Also, enveloping
    multiple specifications from multiple Q's is worth considering.

    Note that this analysis can be time consuming; the time is
    proportional to the number of frequencies multiplied by the number
    of time steps in the signal.

    References
    ----------
    .. [#fde1] "Analysis of Nonstationary Vibroacoustic Flight Data
           Using a Damage-Potential Basis"; S. J. DiMaggio, B. H. Sako,
           S. Rubin; Journal of Spacecraft and Rockets, Vol 40, No. 5,
           September-October 2003.

    .. [#fde2] "Implementing the Fatigue Damage Spectrum and Fatigue
            Damage Equivalent Vibration Testing"; Scot I. McNeill; 79th
            Shock and Vibration Symposium, October 26 – 30, 2008.

    See also
    --------
    :func:`scipy.signal.welch`, :func:`ytools.psdmod`,
    :func:`cyclecount.rainflow`, :func:`srs.srs`.

    Examples
    --------
    Generate 60 second random signal to a pre-defined spec level,
    compute the PSD several different ways and compare. Since it's 60
    seconds, the damage-based PSDs should be fairly close to the input
    spec level. The damage-based PSDs will be calculated with several
    Qs and enveloped.

    In this example, G2 envelopes G1, G4, G8, G12.  This is not always
    the case.  For example, try TF=120; the damage-based curves go up
    fit equal damage in 60s.

    One Count vs. Amp**2 plot is done for illustration.

    .. plot::
        :context: close-figs

        >>> import numpy as np
        >>> import matplotlib.pyplot as plt
        >>> from pyyeti import ytools, fdepsd
        >>> import scipy.signal as signal
        >>>
        >>> TF = 60  # make a 60 second signal
        >>> spec = [[20, 1], [50, 1]]
        >>> sig, sr, t = ytools.psd2time(spec, ppc=10, fstart=20,
        ...                              fstop=50, df=1/TF,
        ...                              winends=dict(portion=10),
        ...                              gettime=True)
        >>>
        >>> fig = plt.figure('fdepsd example, part 1', figsize=[9, 6])
        >>> _ = plt.subplot(211)
        >>> _ = plt.plot(t, sig)
        >>> _ = plt.title(r'Input Signal - Specification Level = '
        ...               '1.0 $g^{2}$/Hz')
        >>> _ = plt.xlabel('Time (sec)')
        >>> _ = plt.ylabel('Acceleration (g)')
        >>> _ = plt.subplot(212)
        >>> f, p = signal.welch(sig, sr, nperseg=sr)
        >>> f2, p2 = ytools.psdmod(4, .5, sig, sr, nperseg=sr)
        >>> plt.tight_layout()

        Calculate G1, G2, and the damage potential PSDs:

        >>> psd = 0
        >>> freq = np.arange(20., 50.1)
        >>> for q in (10, 25, 50):
        ...     fde = fdepsd.fdepsd(sig, sr, freq, q)
        ...     psd = np.fmax(psd, fde.psd)
        >>> #
        >>> spec = np.array(spec).T
        >>> _ = plt.plot(*spec, 'k-', lw=1.5, label='Spec')
        >>> _ = plt.plot(f, p, label='Welch PSD')
        >>> _ = plt.plot(f2, p2, label='PSDmod')
        >>> _ = plt.xlim(20, 50)
        >>> _ = plt.title('PSD Comparison')
        >>> _ = plt.xlabel('Freq (Hz)')
        >>> _ = plt.ylabel(r'PSD ($g^{2}$/Hz)')
        >>> _ = plt.plot(freq, psd[:, 0], label='G1 env')
        >>> _ = plt.plot(freq, psd[:, 1], label='G2 env')
        >>> _ = plt.plot(freq, psd[:, 2], label='G4 env')
        >>> _ = plt.plot(freq, psd[:, 3], label='G8 env')
        >>> _ = plt.plot(freq, psd[:, 4], label='G12 env')
        >>> _ = plt.legend(loc='upper left',
        ...                bbox_to_anchor=(1.02, 1.),
        ...                borderaxespad=0.)
        >>> fig.subplots_adjust(right=0.75)

    .. plot::
        :context: close-figs

        Compare to theoretical bin counts @ 30 Hz:

        >>> _ = plt.figure('fdepsd example, part 2')
        >>> j = np.searchsorted(freq, 30)
        >>> Frq = freq[j]
        >>> _ = plt.semilogy(fde.binamps[j]**2, fde.count[j],
        ...                  label='Data')
        >>> # use flight time here (TF), not test time (T0)
        >>> Amax2 = 2*fde.var[j]*np.log(Frq*TF)
        >>> _ = plt.plot([0, Amax2], [Frq*TF, 1], label='Theory')
        >>> y1 = fde.count[j, 0]
        >>> _ = plt.plot([0, fde.amp[j, 0]**2], [y1, 1], label='G1')
        >>> _ = plt.plot([0, fde.amp[j, 1]**2], [y1, 1], label='G2')
        >>> _ = plt.plot([0, fde.amp[j, 2]**2], [y1, 1], label='G4')
        >>> _ = plt.plot([0, fde.amp[j, 3]**2], [y1, 1], label='G8')
        >>> _ = plt.plot([0, fde.amp[j, 4]**2], [y1, 1], label='G12')
        >>> _ = plt.title('Bin Count Check for Q=50, Freq=30 Hz')
        >>> _ = plt.xlabel(r'$Amp^2$')
        >>> _ = plt.ylabel('Count')
        >>> _ = plt.legend(loc='best')
    """
    sig, freq = np.atleast_1d(sig, freq)
    if sig.ndim > 1 or freq.ndim > 1:
        raise ValueError('`sig` and `freq` must both be 1d arrays')
    if resp not in ('absacce', 'pvelo'):
        raise ValueError("`resp` must be 'absacce' or 'pvelo'")
    (coeffunc, methfunc,
     rollfunc, ptr) = srs._process_inputs(resp, 'abs',
                                          rolloff, 'primary')

    if hpfilter is not None:
        if verbose:
            print('High pass filtering @ {} Hz'.format(hpfilter))
        b, a = signal.butter(3, hpfilter/(sr/2), 'high')
        sig = signal.filtfilt(b, a, sig)

    mxfrq = freq.max()
    curppc = sr/mxfrq
    if rolloff == 'prefilter':
        sig, sr = rollfunc(sig[None, :], sr, ppc, mxfrq)
        sig = sig.flatten()
        rollfunc = None

    if curppc < ppc and rollfunc:
        if verbose:
            print('Using {} method to increase sample rate (have '
                  'only {} pts/cycle @ {} Hz'.
                  format(rolloff, curppc, mxfrq))
        sig, sr = rollfunc(sig[None, :], sr, ppc, mxfrq)
        sig = sig.flatten()
        ppc = sr/mxfrq
        if verbose:
            print('After interpolation, have {} pts/cycle @ {} Hz\n'.
                  format(ppc, mxfrq))

    LF = freq.size
    dT = 1/sr
    pi = np.pi
    Wn = 2*pi*freq
    parallel, ncpu = srs._process_parallel(parallel, LF, sig.size,
                                           maxcpu, getresp=False)
    # allocate RAM:
    if parallel == 'yes':
        # global shared vars will be: WN, SIG, ASV, BinAmps, Count
        WN = (srs.copyToSharedArray(Wn), Wn.shape)
        SIG = (srs.copyToSharedArray(sig), sig.shape)
        ASV = (srs.createSharedArray((3, LF)), (3, LF))
        BinAmps = (srs.createSharedArray((LF, nbins)), (LF, nbins))
        a = _to_np_array(BinAmps)
        a += np.arange(nbins, dtype=float)/nbins
        Count = (srs.createSharedArray((LF, nbins)), (LF, nbins))
        args = (coeffunc, Q, dT, verbose)
        gvars = (WN, SIG, ASV, BinAmps, Count)
        func = _dofde
        with mp.Pool(processes=ncpu,
                     initializer=_mk_par_globals,
                     initargs=gvars) as pool:
            for _ in pool.imap_unordered(
                    func, zip(range(LF), it.repeat(args, LF))):
                pass
        ASV = _to_np_array(ASV)
        Amax = ASV[0]
        SRSmax = ASV[1]
        Var = ASV[2]
        Count = _to_np_array(Count)
        BinAmps = a
    else:
        Amax = np.zeros(LF)
        SRSmax = np.zeros(LF)
        Var = np.zeros(LF)
        BinAmps = np.zeros((LF, nbins))
        BinAmps += np.arange(nbins, dtype=float)/nbins
        Count = np.zeros((LF, nbins))

        # loop over frequencies, calculating responses & counting cycles
        for j, wn in enumerate(Wn):
            if verbose:
                print('Processing frequency {:8.2f} Hz'.
                      format(wn/2/pi), end='\r')
            b, a = coeffunc(Q, dT, wn)
            resphist = signal.lfilter(b, a, sig)
            SRSmax[j] = abs(resphist).max()
            Var[j] = np.var(resphist)

            # use rainflow to count cycles:
            ind = cyclecount.findap(resphist)
            rf = cyclecount.rainflow(resphist[ind])

            amp = rf['amp']
            count = rf['count']
            Amax[j] = amp.max()
            BinAmps[j] *= Amax[j]

            # cumulative bin count:
            for jj in range(nbins):
                pv = amp >= BinAmps[j, jj]
                Count[j, jj] = np.sum(count[pv])

    if verbose:
        print()
        print('Computing outputs G1, G2, etc.')

    # calculate non-cumulative counts per bin:
    BinCount = np.hstack((Count[:, :-1]-Count[:, 1:], Count[:, -1:]))

    # for calculating G2:
    G2max = Amax**2
    for j in range(LF):
        pv = BinAmps[j] >= Amax[j]/3  # ignore small amplitude cycles
        if np.any(pv):
            x = BinAmps[j, pv]**2
            x2 = G2max[j]
            y = np.log(Count[j, pv])
            # y1 = np.log(max(Count[j, 0], freq[j]*T0))
            y1 = np.log(Count[j, 0])
            g1y = np.interp(x, [0, x2], [y1, 0])
            tantheta = (y - g1y)/x
            k = np.argmax(tantheta)
            if tantheta[k] > 0:
                # g2 line is higher than g1 line, so find BinAmps**2
                # where log(count) = 0; ie, solve for x-intercept in
                # y = m x + b; (x, y) pts are: (0, y1), (x[k], y[k]):
                G2max[j] = x[k]*y1/(y1 - y[k])
                # if j == 10:
                #     import matplotlib.pyplot as plt
                #     plt.figure('g2')
                #     plt.clf()
                #     plt.semilogy(BinAmps[j]**2, Count[j],
                #                  label='Data')
                #     plt.plot([0, Amax[j]**2], [np.exp(y1), 1],
                #              label='G1')
                #     plt.plot([0, G2max[j]], [np.exp(y1), 1],
                #              label='G2')
                #     plt.plot(x[k], np.exp(y[k]), 'o', label='g2 pt')
                #     plt.legend(loc='best')

    # calculate flight-damage indicators for b = 4, 8 and 12:
    b4 = 4
    b8 = 8
    b12 = 12
    Df4 = np.zeros(LF)
    Df8 = np.zeros(LF)
    Df12 = np.zeros(LF)
    for j in range(LF):
        Df4[j] = (BinAmps[j]**b4).dot(BinCount[j])
        Df8[j] = (BinAmps[j]**b8).dot(BinCount[j])
        Df12[j] = (BinAmps[j]**b12).dot(BinCount[j])

    N0 = freq*T0
    lnN0 = np.log(N0)
    if resp == 'absacce':
        G1 = Amax**2 / (Q * pi * freq * lnN0)
        G2 = G2max / (Q * pi * freq * lnN0)

        # calculate test-damage indicators for b = 4, 8 and 12:
        Abar = 2*lnN0
        Abar2 = Abar**2
        Dt4 = N0*8 - (Abar2 + 4*Abar + 8)
        sig2_4 = np.sqrt(Df4 / Dt4)
        G4 = sig2_4 / ((Q*pi/2)*freq)

        Abar3 = Abar2*Abar
        Abar4 = Abar2*Abar2
        Dt8 = N0*384 - (Abar4 + 8*Abar3 + 48*Abar2 + 192*Abar + 384)
        sig2_8 = (Df8 / Dt8)**(1/4)
        G8 = sig2_8 / ((Q*pi/2)*freq)

        Abar5 = Abar4*Abar
        Abar6 = Abar4*Abar2
        Dt12 = N0*46080 - (Abar6 + 12*Abar5 + 120*Abar4 + 960*Abar3 +
                           5760*Abar2 + 23040*Abar + 46080)
        sig2_12 = (Df12 / Dt12)**(1/6)
        G12 = sig2_12 / ((Q*pi/2)*freq)

        Gmax = np.sqrt(np.vstack((G4, G8, G12)) * (Q*pi*freq*lnN0))
    else:
        G1 = (Amax**2 * 4 * pi * freq) / (Q * lnN0)
        G2 = (G2max * 4 * pi * freq) / (Q * lnN0)

        Dt4 = 2*N0
        sig2_4 = np.sqrt(Df4 / Dt4)
        G4 = sig2_4 * ((4*pi/Q)*freq)

        Dt8 = 24*N0
        sig2_8 = (Df8 / Dt8)**(1/4)
        G8 = sig2_8 * ((4*pi/Q)*freq)

        Dt12 = 720*N0
        sig2_12 = (Df12 / Dt12)**(1/6)
        G12 = sig2_12 * ((4*pi/Q)*freq)

        Gmax = np.sqrt(np.vstack((G4, G8, G12))*(Q*lnN0)/(4*pi*freq))

    # assemble outputs:
    Gpsd = np.vstack((G1, G2, G4, G8, G12)).T
    G2max = np.sqrt(G2max)
    Gmax = np.vstack((Amax, G2max, Gmax)).T
    return SimpleNamespace(freq=freq, psd=Gpsd, amp=Gmax,
                           binamps=BinAmps, count=Count, var=Var,
                           srs=SRSmax, parallel=parallel, ncpu=ncpu,
                           resp=resp)