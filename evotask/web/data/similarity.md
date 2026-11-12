# 文本相似度 · 入门资料

Cosine similarity measures the angle between two vectors:
cosine(a, b) = dot(a, b) / (norm(a) * norm(b)).

余弦相似度关注方向而不是绝对长度。对文本进行 TF-IDF 向量化后，可以用 cosine similarity 比较词项分布；它不直接理解语义。

## 边界条件

Empty input should produce a zero similarity score rather than division by zero.
空文本和零向量应返回零相似度，避免除零。中文文本需要适合中文的分词，或使用字符 n-gram。

## 验证方法

相同非零文本的相似度应为 1；无共同词项的文本在词袋表示下相似度应为 0。任何实现都应该测试空输入、重复词和中英文混合文本。

这是一份用于演示检索、引用和协作流程的教学资料，不是正式研究结论。
