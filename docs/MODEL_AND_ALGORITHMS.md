# Bài toán FFP và thuật toán SAT

## Mô tả bài toán

Đầu vào là đồ thị vô hướng đơn không trọng số $G=(V,E)$, tập đỉnh cháy ban đầu
không rỗng $B\subseteq V$, và số lính cứu hỏa $D\ge 1$ cho mỗi vòng. Mỗi đỉnh ở
một trong ba trạng thái untouched, burned hoặc defended. Burned và defended là
trạng thái hấp thụ.

Ở đầu vòng $t\ge 1$, bảo vệ tối đa $D$ đỉnh untouched, rồi lửa lan đồng thời từ
các đỉnh burned trước vòng đó tới mọi láng giềng chưa được bảo vệ. Một trạng thái
được contained khi không còn cạnh nối đỉnh burned với đỉnh untouched. Trạng thái
ban đầu cũng có thể contained; khi đó containment time bằng $0$.

Mục tiêu là tối thiểu hóa số đỉnh cháy cuối cùng:

$$
K=|B_{\mathrm{final}}|
$$

Điều này tương đương tối đa hóa số đỉnh được cứu $|V|-K$. Schedule là dãy
$(P_1,\ldots,P_T)$, trong đó $P_t$ là tập đỉnh được bảo vệ ở vòng $t$ và
$|P_t|\le D$.

## Biến SAT

Với mỗi đỉnh $v$ và thời điểm sau vòng $t$, encoding dùng:

| Biến | Nghĩa |
| --- | --- |
| $b_{v,t}$ | $v$ đã cháy sau vòng $t$ |
| $d_{v,t}$ | $v$ đã được bảo vệ trước hoặc trong vòng $t$ |
| $a_{v,t}$ | $v$ được bảo vệ lần đầu ở vòng $t$ |
| $h_T$ | activation literal yêu cầu containment tại horizon $T$ |
| $c^{(T)}_{i,j}$ | auxiliary của saved counter: ít nhất $j$ saved literals đúng trong prefix dài $i$ tại horizon $T$ |

Layer 0 chỉ tạo biến burned/defended; $a_{v,t}$ được tạo từ vòng 1. Activation
$h_T$ chỉ tạo khi chuẩn bị query tại $T$, kể cả $T=0$.
Variable manager cấp ID cho biến semantic, activation và auxiliary theo một counter
chung; `ITotalizer` nhận `top_id` hiện tại để tránh va chạm ID.

## Công thức động học

Điều kiện đầu:

$$
\begin{aligned}
b_{v,0} &= 1 && \text{khi } v\in B,\\
b_{v,0} &= 0 && \text{khi } v\notin B,\\
d_{v,0} &= 0 && \text{với mọi } v.
\end{aligned}
$$

Burned và defended giữ nguyên trạng thái, và không thể cùng đúng:

$$
\begin{aligned}
b_{v,t-1} &\rightarrow b_{v,t},\\
d_{v,t-1} &\rightarrow d_{v,t},\\
\neg b_{v,t} &\lor \neg d_{v,t}.
\end{aligned}
$$

Hành động bảo vệ đúng khi đỉnh chuyển từ untouched sang defended:

$$
a_{v,t}\leftrightarrow\left(d_{v,t}\land\neg d_{v,t-1}\right)
$$

Các clause CNF tương ứng:

$$
\begin{aligned}
\neg a_{v,t}&\lor d_{v,t},\\
\neg a_{v,t}&\lor\neg d_{v,t-1},\\
\neg d_{v,t}&\lor d_{v,t-1}\lor a_{v,t}.
\end{aligned}
$$

Mỗi firefighter chỉ bảo vệ một đỉnh trong một vòng:

$$
\sum_{v\in V}a_{v,t}\le D
$$

Ràng buộc này dùng một `ITotalizer` cho mỗi layer và được thêm cố định vào solver.

Với mỗi cạnh vô hướng $\{u,v\}$, spread được mã hóa theo cả hai hướng. Cho hướng
$u\rightarrow v$:

$$
b_{u,t-1}\land\neg d_{v,t}\rightarrow b_{v,t}
$$

hay trong CNF:

$$
\neg b_{u,t-1}\lor d_{v,t}\lor b_{v,t}
$$

Để cấm cháy tự phát, một đỉnh cháy ở vòng $t$ phải đã cháy từ trước hoặc có ít
nhất một láng giềng cháy ở vòng trước:

$$
b_{v,t}\rightarrow\left(b_{v,t-1}\lor\bigvee_{u\in N(v)}b_{u,t-1}\right)
$$

CNF:

$$
\neg b_{v,t}\lor b_{v,t-1}\lor\bigvee_{u\in N(v)}b_{u,t-1}
$$

Clause spread bắt cháy khi điều kiện lan đúng; clause no-spontaneous cấm encoding
tự gán $b_{v,t}$ để làm sai động học.

## Horizon, containment và objective

Định nghĩa $F(T,K)$: tồn tại schedule sao cho lửa contained không muộn hơn trạng
thái sau vòng $T$ và có tối đa $K$ đỉnh cháy. Với mỗi cạnh vô hướng, kiểm tra cả
hai hướng. Cho hướng $u\rightarrow v$, trạng thái contained yêu cầu:

$$
b_{u,T}\rightarrow(b_{v,T}\lor d_{v,T})
$$

Clause CNF được kích hoạt bởi $h_T$:

$$
\neg h_T\lor\neg b_{u,T}\lor b_{v,T}\lor d_{v,T}
$$

Dynamics được nối tới horizon đang cần; containment chỉ được tạo khi horizon đó
được chuẩn bị để query. Mỗi horizon được tạo đúng một activation và $2m$ clause.
Query SAT đưa $h_T$ hiện tại cùng $\neg h_{\mathrm{old}}$ cho các horizon khác
đã tạo vào assumptions, nên containment cũ không áp dụng ở query mới. Cách định nghĩa này cũng dùng cho simulator
và heuristic. Containment time mô phỏng có thể nhỏ hơn horizon SAT.

Objective là số đỉnh cháy ở horizon $T$:

$$
\sum_{v\in V}b_{v,T}\le K
$$

Mỗi horizon được query có một objective manager riêng. Đặt $q=n-K$ và
$x_i=\neg b_{v_i,T}$, với vertex order cố định $v_i=i-1$. Khi $K>n//2$,
objective tương đương:

$$
\sum_{i=1}^{n}x_i\ge q.
$$

Phía saved dùng counter incremental với các auxiliary variables:

$$
c_{i,j}\iff\sum_{r=1}^{i}x_r\ge j,
\qquad 1\le j\le\min(i,q_{\max}).
$$

Với $1<j<i$, recurrence là:

$$
y\leftrightarrow A\lor(B\land x_i),
\quad y=c_{i,j},\ A=c_{i-1,j},\ B=c_{i-1,j-1}.
$$

Bốn clause encode equivalence đầy đủ:

$$
(\neg A\lor y)\land
(\neg B\lor\neg x_i\lor y)\land
(\neg y\lor A\lor B)\land
(\neg y\lor A\lor x_i).
$$

Boundary $c_{1,1}\leftrightarrow x_1$ dùng hai clause:

$$
(\neg c_{1,1}\lor x_1)\land(\neg x_1\lor c_{1,1}).
$$

Với $i>1$, boundary $j=1$ là $c_{i,1}\leftrightarrow(c_{i-1,1}\lor x_i)$:

$$
(\neg c_{i-1,1}\lor c_{i,1})\land
(\neg x_i\lor c_{i,1})\land
(\neg c_{i,1}\lor c_{i-1,1}\lor x_i).
$$

Boundary $j=i>1$ là $c_{i,i}\leftrightarrow(c_{i-1,i-1}\land x_i)$:

$$
(\neg c_{i,i}\lor c_{i-1,i-1})\land
(\neg c_{i,i}\lor x_i)\land
(\neg c_{i-1,i-1}\lor\neg x_i\lor c_{i,i}).
$$

Query AtLeast $q$ chỉ assume $c_{n,q}$; $q=0$ không cần literal. Khi threshold
đổi từ $q$ sang $q+1$, chỉ tạo column mới, không rebuild và không sửa clause cũ.
Việc encode cả hai chiều là cần thiết để output dương phản ánh đúng saved count.
Không dùng output dương của `ITotalizer` làm AtLeast.

Khi $K\le n//2$, phía burned dùng `ITotalizer` với assumption `-rhs[K]`.
Capacity của totalizer được tăng incremental nếu cần query bound lớn hơn đã tạo.
Firefighter cardinality vẫn dùng implementation `AtMost` cũ. Hai phía objective
có thể cùng tồn tại; mỗi query chỉ assume output của phía được chọn. Không có
permanent objective bound. Auxiliary IDs của cả hai phía dùng cùng `VarManager`,
tách biệt với bộ đếm semantic và containment activation.

Encoder chỉ tạo layer tới horizon đang cần và nối thêm khi $T$ tăng.

## Preprocessing: lower bound và horizon tối đa

Multi-source BFS từ $B$ tính khoảng cách ngắn nhất $\delta(v)$. Lửa không thể tới
$v$ trước vòng này, do đó khi $t<\delta(v)$ thêm unit clause $\neg b_{v,t}$.
Đỉnh không reachable có $\delta(v)=\infty$ và không thể tự cháy do các clause động
học.

Lower bound rẻ:

$$
L=|B|+\max\left(0,\left|N(B)\setminus B\right|-D\right)
$$

Ít nhất $|B|$ đỉnh cháy ban đầu; vòng đầu bảo vệ được tối đa $D$ láng giềng đang
bị đe dọa. Bound này không phụ thuộc vào UNSAT ở horizon ngắn.

Certification horizon cho query cải thiện incumbent:

$$
\begin{aligned}
T_{\mathrm{old}}&=\left\lceil\frac{n}{D}\right\rceil,\\
T_{\mathrm{struct}}&=\left\lceil\frac{n-|B|}{D+1}\right\rceil,\\
T_{\mathrm{UB}}&=U-|B|,\\
T_{\mathrm{LB}}&=\left\lfloor\frac{n-L}{D}\right\rfloor+1,\\
T_{\mathrm{cert}}&=\min(T_{\mathrm{struct}},T_{\mathrm{UB}},T_{\mathrm{LB}}).
\end{aligned}
$$

Chọn nghiệm tối ưu canonical bảo vệ đủ $D$ đỉnh ở các vòng trước vòng cuối. Việc
bảo vệ thêm đỉnh untouched không làm tăng số đỉnh cháy. Nếu còn ít hơn $D$ đỉnh
untouched thì bảo vệ hết sẽ contained ngay, nên vòng đó là vòng cuối.

Mỗi vòng trước vòng cuối cũng phải làm cháy ít nhất một đỉnh mới. Sau $T-1$ vòng,
có ít nhất $|B|+(D+1)(T-1)$ đỉnh cháy hoặc defended, và còn ít nhất một đỉnh
untouched. Suy ra cận cấu trúc. Với nghiệm cải thiện, $K^*\le U-1$ và
$K^*\ge |B|+T-1$, suy ra cận UB. Các đỉnh defended rời nhau với tập cháy cuối;
$L+D(T-1)\le n$ cho cận LB. Cả ba cận áp dụng cho cùng một nghiệm tối ưu canonical.

Cận cũ chỉ dùng chẩn đoán. Các cận mới dùng số nguyên và được tính lại khi $U$ hoặc
$L$ thay đổi. Nếu một nghiệm tốt hơn tồn tại thì có nghiệm tối ưu contained không
muộn hơn $T_{\mathrm{cert}}$. Vì vậy UNSAT cho $F(T,U-1)$ khi
$T\ge T_{\mathrm{cert}}$ chứng minh incumbent tối ưu.

## Heuristic tạo incumbent

Heuristic `Threat` bảo vệ tối đa $D$ đỉnh đang bị đe dọa theo thứ tự ID. Dùng ký hiệu
$B_t$ cho tập burned và $R_t$ cho tập defended sau vòng $t$. Tập threatened đầu vòng
tiếp theo là:

$$
C_{t+1}=\left(\bigcup_{u\in B_t}N(u)\right)\setminus(B_t\cup R_t)
$$

Chọn $P_{t+1}\subseteq C_{t+1}$, $|P_{t+1}|\le D$, rồi cập nhật:

$$
\begin{aligned}
R_{t+1}&=R_t\cup P_{t+1},\\
B_{t+1}&=B_t\cup(C_{t+1}\setminus P_{t+1}).
\end{aligned}
$$

Sau deterministic `Threat`, portfolio chạy `DegreeThreat` và các lần
`RandomizedThreat`. DegreeThreat xếp theo degree giảm dần rồi ID; RandomizedThreat
lấy ngẫu nhiên $D$ đỉnh khi threatened set lớn hơn $D$, dùng seed CLI.

Mỗi nghiệm đi qua mô phỏng heuristic để có $K$ và containment time. Incumbent chọn
$K$ nhỏ nhất, sau đó containment sớm nhất và schedule từ điển khi hòa. Pareto
frontier gộp bản sao; nghiệm $i$ dominates $j$ khi $T_i\le T_j$, $K_i\le K_j$ và ít
nhất một bất đẳng thức nghiêm.

Ngân sách heuristic mặc định là $\min(5\text{ s},0.05\,\texttt{time\_limit})$ và
nằm trong deadline chung. Heuristic không có bảo đảm tối ưu.

## Tìm kiếm tối ưu tuần tự

Worker giữ một solver CaDiCaL xuyên suốt. Gọi $U$ là $K$ incumbent, $L$ là lower
bound và $T$ là horizon hiện tại. Khởi tạo:

$$
T_0=\min\left(T_{\mathrm{cert}},\left\lceil cT_{\mathrm{inc}}\right\rceil\right),
\qquad c=1.5
$$

Trong đó $T_{\mathrm{inc}}$ là containment time của incumbent. Dùng ceiling; ví dụ
$T_{\mathrm{inc}}=7$ cho $T_0=11$ nếu certification horizon đủ lớn. Tùy chọn
`--initial-horizon-factor` điều khiển $c\ge1$. Tùy chọn `--horizon-growth-factor`
điều khiển $g\ge1$, mặc định $g=2$. Hai hệ số chỉ quyết định hiệu năng và được ghi
trong JSON/CSV. Nếu $L=U$, trả tối ưu trước khi tạo solver; trạng thái đầu contained
được trả tại $T=0$.

Nếu chưa, query tuần tự từ UB:

$$
F(T,U-1)=\mathrm{SAT}?
$$

- SAT: giải mã schedule và cập nhật $U$ bằng số đỉnh cháy trong model; tiếp tục thử
  $F(T,U-1)$ ở cùng horizon sau khi tính lại certification horizon. Không giảm $T$
  khi cận co lại và không rebuild solver.
- UNSAT khi $T<T_{\mathrm{cert}}$: không tăng lower bound toàn cục. Thử horizon
  $T'=\min(T_{\mathrm{cert}},\max(T+1,\lceil gT\rceil))$.
- UNSAT khi $T\ge T_{\mathrm{cert}}$: đặt $L=U$ và kết luận `OPTIMAL`.
- Timeout: trả checkpoint mới nhất. `best_k=upper_bound=U` là số đỉnh cháy của
  schedule incumbent đã tìm được; query đang chạy không được xem là UNSAT.

Sau worker hoàn thành hoặc bị dừng, parent chạy trusted simulator một lần trên
schedule cuối. Nếu simulator cho $K$ khác giá trị báo cáo hoặc containment time lớn
hơn `incumbent_horizon`, kết quả thành `ERROR`, `reason=MODEL_VALIDATION_FAILED`.
Worker đã lỗi vẫn giữ trạng thái ERROR dù schedule checkpoint hợp lệ. Validation có
`final_validation_time` riêng và không tính vào `elapsed_total` hay `solve_time`.
