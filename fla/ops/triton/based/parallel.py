
# -*- coding: utf-8 -*-

import torch
import triton
import triton.language as tl

from fla.ops.triton.utils import contiguous

# Based: An Educational and Effective Sequence Mixer
# https://hazyresearch.stanford.edu/blog/2023-12-11-zoology2-based


@triton.jit
def parallel_based_fwd_kernel(
    # B: batch_size, H: n_heads, T: seq_len, D: d_head
    q,  # query [B, H, L, D_head_K]
    k,  # key [B, H, L, D_head_V]
    v,  # value [B, H, L, D_head_V]
    o,  # output [B, H, L, D_head_V]
    s_qk_h,  # stride size: L * D_head_K
    s_qk_t,  # stride size: D_head_K
    s_qk_d,  # stride size: 1
    s_vo_h,  # stride size: L * D_head_V
    s_vo_t,  # stride size: D_head_V
    s_vo_d,  # stride size: 1
    B,  # batch size
    H,  # n_heads
    T,  # seq_len
    scale,  # D_head_K ** -0.5
    BTL: tl.constexpr,  # BLOCK SIZE along the sequence dimension for Q
    BTS: tl.constexpr,  # BLOCK SIZE along the sequence dimension for K/V
    BK: tl.constexpr,  # BLOCK SIZE along the K dimension
    BV: tl.constexpr,  # BLOCK SIZE along the V dimension
    DK: tl.constexpr,  # D_head_K
    DV: tl.constexpr,  # D_head_V
):
    # i_c: chunk index. used for sequence parallelism
    i_kv, i_c, i_bh = tl.program_id(0), tl.program_id(1), tl.program_id(2)
    NV = tl.cdiv(DV, BV)
    i_k = i_kv // (NV)
    i_v = i_kv % (NV)

    p_q = tl.make_block_ptr(q + i_bh * s_qk_h, (T, DK),
                            (s_qk_t, s_qk_d), (i_c * BTL, i_k * BK), (BTL, BK), (1, 0))
    p_k = tl.make_block_ptr(k + i_bh * s_qk_h, (DK, T),
                            (s_qk_d, s_qk_t), (i_k * BK, 0), (BK, BTS), (0, 1))
    p_v = tl.make_block_ptr(v + i_bh * s_vo_h, (T, DV),
                            (s_vo_t, s_vo_d), (0, i_v * BV), (BTS, BV), (1, 0))

    # [BQ, BD] block Q, in the shared memory throughout the whole kernel
    b_q = tl.load(p_q, boundary_check=(0, 1))
    b_q = (b_q * scale).to(b_q.dtype)
    b_o = tl.zeros([BTL, BV], dtype=tl.float32)

    # Q block and K block have no overlap
    # no need for mask, thereby saving flops
    for _ in range(0, i_c * BTL, BTS):
        # [BK, BTS]
        b_k = tl.load(p_k, boundary_check=(0, 1))
        # [BTS, BV]
        b_v = tl.load(p_v, boundary_check=(0, 1))
        # [BTL, BTS]
        b_s = tl.dot(b_q, (b_k), allow_tf32=False)
        b_s = 1 + b_s + 0.5 * b_s * b_s
        # [BQ, BD]
        b_o = b_o + tl.dot(b_s.to(b_v.dtype), b_v, allow_tf32=False)
        p_k = tl.advance(p_k, (0, BTS))
        p_v = tl.advance(p_v, (BTS, 0))

    # # rescale interchunk output
    tl.debug_barrier()
    o_q = tl.arange(0, BTL)
    # # sync threads, easy for compiler to optimize
    # tl.debug_barrier()

    o_k = tl.arange(0, BTS)
    p_k = tl.make_block_ptr(k + i_bh * s_qk_h, (DK, T),
                            (s_qk_d, s_qk_t), (i_k * BK, i_c * BTL), (BK, BTS), (0, 1))
    p_v = tl.make_block_ptr(v + i_bh * s_vo_h, (T, DV),
                            (s_vo_t, s_vo_d), (i_c * BTL, i_v * BV), (BTS, BV), (1, 0))
    # Q block and K block have overlap. masks required
    for _ in range(i_c * BTL, (i_c + 1) * BTL, BTS):
        # [BK, BTS]
        b_k = tl.load(p_k, boundary_check=(0, 1))
        # [BTS, BV]
        b_v = tl.load(p_v, boundary_check=(0, 1))
        # [BTL, BTS]
        m_s = o_q[:, None] >= o_k[None, :]
        b_s = tl.dot(b_q, b_k, allow_tf32=False)
        b_s = 1 + b_s + 0.5 * b_s * b_s
        b_s = tl.where(m_s, b_s, 0)
        # [BTL, BV]
        b_o += tl.dot(b_s.to(b_q.dtype), b_v, allow_tf32=False)

        p_k = tl.advance(p_k, (0, BTS))
        p_v = tl.advance(p_v, (BTS, 0))
        o_k += BTS

    p_o = tl.make_block_ptr(o + (i_bh + B * H * i_k) * s_vo_h, (T, DV),
                            (s_vo_t, s_vo_d), (i_c*BTL, i_v*BV), (BTL, BV), (1, 0))
    tl.store(p_o, b_o.to(p_o.dtype.element_ty), boundary_check=(0, 1))


@triton.jit
def _parallel_based_bwd_dq(
    i_bh, i_c, i_k, i_v, i_h,
    q, k, v, do, dq, s_qk_h, s_qk_t, s_qk_d, s_vo_h,
    s_vo_t, s_vo_d, B, H, T, scale,
    BTL: tl.constexpr, BTS: tl.constexpr, BK: tl.constexpr, BV: tl.constexpr,
    DK: tl.constexpr,  DV: tl.constexpr,
):
    p_do = tl.make_block_ptr(do + i_bh * s_vo_h, (T, DV), (s_vo_t, s_vo_d),
                             (i_c * BTL, i_v * BV), (BTL, BV), (1, 0))
    p_q = tl.make_block_ptr(q + (i_bh) * s_qk_h, (T, DK),
                            (s_qk_t, s_qk_d), (i_c*BTL, i_k*BK), (BTL, BK), (1, 0))
    b_q = tl.load(p_q, boundary_check=(0, 1))
    b_q = (b_q * scale).to(b_q.dtype)
    b_do = tl.load(p_do, boundary_check=(0, 1))
    b_dq = tl.zeros([BTL, BK], dtype=tl.float32)
    p_k = tl.make_block_ptr(k + i_bh * s_qk_h, (T, DK),
                            (s_qk_t, s_qk_d), (0, i_k * BK), (BTS, BK), (1, 0))
    p_v = tl.make_block_ptr(v + i_bh * s_vo_h, (DV, T),
                            (s_vo_d, s_vo_t), (i_v * BV, 0), (BV, BTS), (0, 1))
    for _ in range(0, i_c * BTL, BTS):
        # [BTS, BK]
        b_k = tl.load(p_k, boundary_check=(0, 1))
        # [BV, BTS]
        b_v = tl.load(p_v, boundary_check=(0, 1))
        # [BTL, BTS]
        b_ds = tl.dot(b_do, b_v, allow_tf32=False)
        b_s = tl.dot(b_q, tl.trans(b_k), allow_tf32=False)
        # [BQ, BD]
        b_dq += tl.dot((b_ds * (1 + b_s)).to(b_v.dtype), b_k, allow_tf32=False)
        p_k = tl.advance(p_k, (BTS, 0))
        p_v = tl.advance(p_v, (0, BTS))

    b_dq *= scale
    o_q = tl.arange(0, BTL)
    o_k = tl.arange(0, BTS)
    p_k = tl.make_block_ptr(k + i_bh * s_qk_h, (T, DK),
                            (s_qk_t, s_qk_d), (i_c * BTL, i_k * BK), (BTS, BK), (1, 0))
    p_v = tl.make_block_ptr(v + i_bh * s_vo_h, (DV, T),
                            (s_vo_d, s_vo_t), (i_v * BV, i_c * BTL), (BV, BTS), (0, 1))
    # Q block and K block have overlap. masks required
    for _ in range(i_c * BTL, (i_c + 1) * BTL, BTS):
        # [BTS, BK]
        b_k = tl.load(p_k, boundary_check=(0, 1))
        # [BV, BTS]
        b_v = tl.load(p_v, boundary_check=(0, 1))
        # [BTL, BTS]
        m_s = o_q[:, None] >= o_k[None, :]
        b_ds = tl.dot(b_do, b_v, allow_tf32=False) * scale
        b_ds = tl.where(m_s, b_ds, 0)
        b_s = tl.dot(b_q, tl.trans(b_k), allow_tf32=False)
        b_s = tl.where(m_s, b_s, 0)
        # [BTL, BK]
        b_dq += tl.dot((b_ds + b_ds * b_s).to(b_k.dtype),
                       b_k, allow_tf32=False)
        p_k = tl.advance(p_k, (BTS, 0))
        p_v = tl.advance(p_v, (0, BTS))
        o_k += BTS
    p_dq = tl.make_block_ptr(dq + (i_bh + B * H * i_v) * s_qk_h, (T, DK),
                             (s_qk_t, s_qk_d), (i_c*BTL, i_k*BK), (BTL, BK), (1, 0))
    tl.store(p_dq, b_dq.to(p_dq.dtype.element_ty), boundary_check=(0, 1))
    return


@triton.jit
def _parallel_based_bwd_dkv(
    i_bh, i_c, i_k, i_v, i_h,
    q, k, v, do, dk, dv, s_qk_h, s_qk_t, s_qk_d, s_vo_h,
    s_vo_t, s_vo_d, B, H, T, scale,
    BTL: tl.constexpr, BTS: tl.constexpr, BK: tl.constexpr, BV: tl.constexpr,
    DK: tl.constexpr,  DV: tl.constexpr,
):
    # compute dk dv
    p_k = tl.make_block_ptr(k + i_bh * s_qk_h, (T, DK), (s_qk_t, s_qk_d),
                            (i_c * BTL, i_k * BK), (BTL, BK), (1, 0))
    p_v = tl.make_block_ptr(v + i_bh * s_vo_h, (T, DV), (s_vo_t, s_vo_d),
                            (i_c * BTL, i_v * BV), (BTL, BV), (1, 0))
    b_k, b_v = tl.load(p_k, boundary_check=(0, 1)), tl.load(
        p_v, boundary_check=(0, 1))
    b_dk, b_dv = tl.zeros([BTL, BK], dtype=tl.float32), tl.zeros(
        [BTL, BV], dtype=tl.float32)
    for i in range((tl.cdiv(T, BTS) * BTS)-BTS, (i_c + 1) * BTL - BTS, -BTS):
        p_q = tl.make_block_ptr(
            q + i_bh * s_qk_h, (DK, T), (s_qk_d, s_qk_t), (i_k * BK, i), (BK, BTS), (0, 1))
        p_do = tl.make_block_ptr(
            do + i_bh * s_vo_h, (DV, T), (s_vo_d, s_vo_t), (i_v * BV, i), (BV, BTS), (0, 1))
        b_q = tl.load(p_q, boundary_check=(0, 1))  # [BK, BTS]
        b_do = tl.load(p_do, boundary_check=(0, 1))  # [BV, BTS]
        b_s = tl.dot(b_k.to(b_q.dtype), b_q, allow_tf32=False) * \
            scale  # [BTL, BTS]
        b_s2 = 1 + b_s + 0.5 * b_s * b_s
        b_dv += tl.dot(b_s2.to(b_q.dtype), tl.trans(b_do), allow_tf32=False)
        b_ds = tl.dot(b_v, b_do, allow_tf32=False) * scale
        b_dk += tl.dot((b_ds + b_ds * b_s).to(b_q.dtype),
                       tl.trans(b_q), allow_tf32=False)

    tl.debug_barrier()
    o_q, o_k = tl.arange(0, BTS), tl.arange(0, BTL)
    for i in range(i_c*BTL, (i_c+1)*BTL, BTS):
        p_q = tl.make_block_ptr(
            q + i_bh * s_qk_h, (DK, T), (s_qk_d, s_qk_t), (i_k * BK, i), (BK, BTS), (0, 1))
        p_do = tl.make_block_ptr(
            do + i_bh * s_vo_h, (DV, T), (s_vo_d, s_vo_t), (i_v * BV, i), (BV, BTS), (0, 1))
        b_q = tl.load(p_q, boundary_check=(0, 1))  # [BD, BQ]
        b_do = tl.load(p_do, boundary_check=(0, 1))
        # [BK, BQ]
        m_s = o_k[:, None] <= o_q[None, :]
        b_s = tl.dot(b_k, b_q, allow_tf32=False) * scale
        b_s2 = 1 + b_s + 0.5 * b_s * b_s
        b_s = tl.where(m_s, b_s, 0)
        b_s2 = tl.where(m_s, b_s2, 0)

        b_ds = tl.dot(b_v, b_do, allow_tf32=False)
        b_ds = tl.where(m_s, b_ds, 0) * scale
        # [BK, BD]
        b_dv += tl.dot(b_s2.to(b_q.dtype), tl.trans(b_do), allow_tf32=False)
        b_dk += tl.dot((b_ds + b_ds * b_s).to(b_q.dtype),
                       tl.trans(b_q), allow_tf32=False)
        o_q += BTS

    p_dk = tl.make_block_ptr(dk + (i_bh + B * H * i_v) * s_qk_h,
                             (T, DK), (s_qk_t, s_qk_d), (i_c*BTL, i_k*BK), (BTL, BK), (1, 0))
    p_dv = tl.make_block_ptr(dv + (i_bh + B * H * i_k) * s_vo_h,
                             (T, DV), (s_vo_t, s_vo_d), (i_c*BTL, i_v*BV), (BTL, BV), (1, 0))
    tl.store(p_dk, b_dk.to(p_dk.dtype.element_ty), boundary_check=(0, 1))
    tl.store(p_dv, b_dv.to(p_dv.dtype.element_ty), boundary_check=(0, 1))
    return


@triton.jit
def parallel_based_bwd_kernel(
    q, k, v, do, dq, dk, dv, s_qk_h, s_qk_t, s_qk_d, s_vo_h,
    s_vo_t, s_vo_d, B, H, T, scale,
    BTL: tl.constexpr, BTS: tl.constexpr, BK: tl.constexpr, BV: tl.constexpr,
    DK: tl.constexpr,  DV: tl.constexpr,
):
    i_kv, i_c, i_bh = tl.program_id(0), tl.program_id(1), tl.program_id(2)
    NV = tl.cdiv(DV, BV)
    i_k = i_kv // (NV)
    i_v = i_kv % (NV)
    i_h = i_bh % H
    _parallel_based_bwd_dq(
        i_bh, i_c, i_k, i_v, i_h,
        q, k, v, do, dq, s_qk_h, s_qk_t, s_qk_d, s_vo_h,
        s_vo_t, s_vo_d, B, H, T, scale,  BTL=BTL, BTS=BTS, BK=BK, BV=BV, DK=DK, DV=DV
    )
    tl.debug_barrier()
    _parallel_based_bwd_dkv(
        i_bh, i_c, i_k, i_v, i_h,
        q, k, v, do, dk, dv, s_qk_h, s_qk_t, s_qk_d, s_vo_h,
        s_vo_t, s_vo_d, B, H, T, scale, BTL, BTS, BK, BV, DK, DV
    )


class ParallelBasedFunction(torch.autograd.Function):
    @staticmethod
    @contiguous
    def forward(ctx, q, k, v):
        q, k, v = q.contiguous(), k.contiguous(), v.contiguous()
        BTL, BTS = 128, 32
        assert BTL % BTS == 0
        assert q.shape[-1] % 16 == 0
        BK = min(128, triton.next_power_of_2(k.shape[-1]))
        BV = min(128, triton.next_power_of_2(v.shape[-1]))
        batch_size, n_heads, seq_len, d_head_qk = q.shape
        d_head_v = v.shape[-1]
        num_stages = 3 if d_head_qk <= 64 else 2
        num_warps = 4
        NK = triton.cdiv(d_head_qk, BK)
        NV = triton.cdiv(d_head_v, BV)
        grid = (NK * NV, triton.cdiv(seq_len, BTL), batch_size * n_heads)
        scale = d_head_qk ** -0.5
        o = torch.empty(NK, batch_size, n_heads, seq_len,
                        d_head_v, dtype=q.dtype, device=q.device)
        parallel_based_fwd_kernel[grid](
            q, k, v, o,
            q.stride(1), q.stride(2), q.stride(3),
            v.stride(1), v.stride(2), v.stride(3),
            batch_size, n_heads, seq_len, scale,
            BTL=BTL, BTS=BTS, BK=BK, BV=BV, DK=d_head_qk, DV=d_head_v,
            num_warps=num_warps,
            num_stages=num_stages
        )
        ctx.save_for_backward(q, k, v)
        return o.sum(0)

    @staticmethod
    @contiguous
    def backward(ctx, do):
        do = do.contiguous()
        q, k, v = ctx.saved_tensors
        BTL, BTS = 64, 32
        assert BTL % BTS == 0
        BK = min(128, triton.next_power_of_2(k.shape[-1]))
        BV = min(128, triton.next_power_of_2(v.shape[-1]))
        batch_size, n_heads, seq_len, d_head_qk = q.shape
        d_head_v = v.shape[-1]
        num_stages = 3 if d_head_qk <= 64 else 2
        num_warps = 4
        NK = triton.cdiv(d_head_qk, BK)
        NV = triton.cdiv(d_head_v, BV)
        grid = (NK * NV, triton.cdiv(seq_len, BTL), batch_size * n_heads)
        scale = d_head_qk ** -0.5

        dq = torch.zeros(NV, batch_size, n_heads, seq_len,
                         d_head_qk, dtype=q.dtype, device=q.device)
        dk = torch.empty(NV, batch_size, n_heads, seq_len,
                         d_head_qk, dtype=q.dtype, device=q.device)
        dv = torch.empty(NK, batch_size, n_heads, seq_len,
                         d_head_v, dtype=q.dtype, device=q.device)

        parallel_based_bwd_kernel[grid](
            q, k, v, do, dq, dk, dv,
            q.stride(1), q.stride(2), q.stride(3),
            v.stride(1), v.stride(2), v.stride(3),
            batch_size, n_heads, seq_len, scale,
            BTL=BTL, BTS=BTS, BK=BK, BV=BV, DK=d_head_qk, DV=d_head_v,
            num_warps=num_warps,
            num_stages=num_stages
        )

        return dq.sum(0), dk.sum(0), dv.sum(0)


parallel_based = ParallelBasedFunction.apply
