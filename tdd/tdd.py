from __future__ import annotations
from typing import Iterable, Tuple, List, Any, Union, cast
import numpy as np
import torch

from . import CUDAcpl, weighted_node
from .CUDAcpl import _U_, CUDAcpl_Tensor, CUDAcpl2np
from . import node
from .node import  TERMINAL_ID, Node, IndexOrder, order_inverse
from .weighted_node import isequal, to_CUDAcpl_Tensor

import copy


from graphviz import Digraph
from IPython.display import Image




class TDD:
    '''
        TDD  functions as the compact representation of tensors,
        and can fit into tensor networks.
    '''
    def __init__(self, 
                    weights: CUDAcpl_Tensor,
                    data_shape: List[int],
                    node: Node|None,
                    index_order: IndexOrder = []):
        self.weights: CUDAcpl_Tensor = weights
        self.data_shape: List[int] = data_shape  #the data index shape
        self.node: Node|None = node

        '''
            index_order: how the inner index are mapped to outer representations
            for example, tdd[a,b,c] under index_order=[0,2,1] returns the value tdd[a,c,b]
            index_order == None means the trival index mapping [0,1,2,(...)]
        '''
        self.index_order: IndexOrder = index_order
    @property
    def parallel_shape(self) -> List[int]:
        return list(self.weights.shape[:-1])

    @property
    def global_order(self)-> List[int]:
        '''
            Return the index order containing both parallel and data indices.
            Note that the last index reserved for CUDA complex is not included
        '''
        parallel_index_order = [i for i in range(len(self.parallel_shape))]
        increment = len(self.parallel_shape)
        return parallel_index_order + [order+increment for order in self.index_order]

    def __eq__(self, other: TDD) -> bool:
        '''
            Now this equality check only deals with TDDs with the same index order.
        '''
        res = self.index_order==other.index_order \
            and isequal(self.node,self.weights,other.node,other.weights)
        return res

    @staticmethod
    def __as_tensor_iterate(tensor : CUDAcpl_Tensor, 
                    parallel_shape: List[int],
                    index_order: List[int], depth: int) -> TDD:
        '''
            The inner interation for as_tensor.
            depth: current iteration depth, used to indicate index_order and termination

            Guarantee: parallel_shape and index_order will not be modified.
        '''

        data_shape = list(tensor.shape[len(parallel_shape):-1])  #the data index shape
        if index_order == []:
            index_order = list(range(len(data_shape)))

        #checks whether the tensor is reduced to the [[...[val]...]] form
        if depth == len(data_shape):

            #maybe some improvement is needed here.
            if len(data_shape)==0:
                weights = tensor.clone()
            else:
                weights = (tensor[...,0:1,:]).clone().detach().view(parallel_shape+[2])
            res = TDD(weights,[],None,[])
            return res
        

        split_pos=index_order[depth]
        split_tensor = list(tensor.split(1,-len(data_shape)+split_pos-1))
            #-1 is because the extra inner dim for real and imag

        the_successors: List[TDD] =[]

        for k in range(data_shape[split_pos]):
            res = TDD.__as_tensor_iterate(split_tensor[k],parallel_shape,index_order,depth+1)
            the_successors.append(res)

        #stack the sub-tdd
        succ_nodes = [item.node for item in the_successors]
        out_weights = torch.stack([item.weights for item in the_successors])
        temp_node = Node(0, depth, out_weights, succ_nodes)
        dangle_weights = CUDAcpl.ones(out_weights.shape[1:-1])
        #normalize at this depth
        new_node, new_dangle_weights = weighted_node.normalized(temp_node, dangle_weights, False)
        tdd = TDD(new_dangle_weights, [], new_node, [])

        return tdd


    @staticmethod
    def as_tensor(data : CUDAcpl_Tensor|np.ndarray|Tuple) -> TDD:
        '''
        construct the tdd tensor

        tensor:
            1. in the form of a matrix only: assume the parallel index and index order to be []
            2. in the form of a tuple (data, index_shape, index_order)
            Note that if the input matrix is a torch tensor, 
                    then it must be already in CUDAcpl_Tensor(CUDA complex) form.
        '''

        if isinstance(data,Tuple):
            tensor,parallel_shape,index_order = data
        else:
            tensor = data
            parallel_shape = []
            index_order: List[int] = []
            
        if isinstance(tensor,np.ndarray):
            tensor = CUDAcpl.np2CUDAcpl(tensor)

        #pre-process above

        data_shape = list(tensor.shape[len(parallel_shape):-1])  #the data index shape
        if index_order == []:
            result_index_order = list(range(len(data_shape)))
        else:
            result_index_order = index_order.copy()


        if len(data_shape)!=len(result_index_order):
            raise Exception('The number of indices must match that provided by tensor.')

        '''
            This extra layer is also for copying the input list and pre-process.
        '''
        res = TDD.__as_tensor_iterate(tensor,parallel_shape,result_index_order,0)

        
        res.index_order = result_index_order
        res.data_shape = data_shape
        return res

    
            
    def CUDAcpl(self) -> CUDAcpl_Tensor:
        '''
            Transform this tensor to a CUDA complex and return.
        '''
        trival_ordered_data_shape = [self.data_shape[i] for i in order_inverse(self.index_order)]
        node_data = to_CUDAcpl_Tensor(self.node,self.weights,trival_ordered_data_shape)
        
        #permute to the right index order
        node_data = node_data.permute(tuple(self.global_order+[node_data.dim()-1]))

        expanded_weights = self.weights.view(tuple(self.parallel_shape)+(1,)*len(self.data_shape)+(2,))
        expanded_weights = expanded_weights.expand_as(node_data)

        return CUDAcpl.einsum('...,...->...',node_data,expanded_weights)
        

    def numpy(self) -> np.ndarray:
        '''
            Transform this tensor to a numpy ndarry and return.
        '''
        return CUDAcpl2np(self.CUDAcpl())


    def clone(self) -> TDD:
        return TDD(self.weights.clone(), self.data_shape.copy(), self.node, self.index_order.copy())

        '''
    
    def __getitem__(self, key) -> TDD:
        Index on the data dimensions.

        Note that only limited form of indexing is allowed here.
        if not isinstance(key, int):
            raise Exception('Indexing form not supported.')
        
        # index by a integer
        inner_index = self.index_order.index(key) #get the corresponding index inside tdd
        node = self.node.
        '''
    
    def __index_single(self, inner_index: int, key: int) -> TDD:
        '''
            Indexing on the single index. Again, inner_index indicate that of tdd nodes DIRECTLY.
        '''
        return self

    
    def __index(self, inner_indices: Tuple[Tuple[int,int]]) -> TDD:
        '''
            Return the indexed tdd according to the chosen keys at given indices.

            Note that here inner_indices indicates that of tdd nodes DIRECTLY.

            indices: [(index1, key1), (index2, key2), ...]
        '''
        #indexing = list(inner_indices).sort(key=lambda item: item[0])
        if inner_indices == ():
            return self.clone()
        return self

        





    def show(self,real_label: bool=True,path: str='output', full_output: bool = False):
        '''
            full_output: if True, then the edge will appear as a tensor, not the parallel index shape.

            (NO TYPING SYSTEM VERIFICATION)
        '''
        edge=[]              
        dot=Digraph(name='reduced_tree')
        dot=Node.layout(self.node,self.parallel_shape,self.index_order, dot,edge, real_label, full_output)
        dot.node('-0','',shape='none')

        if self.node == None:
            id_str = str(TERMINAL_ID)
        else:
            id_str = str(self.node.id)

        if list(self.weights.shape)==[2]:
            dot.edge('-0',id_str,color="blue",label=
                str(complex(round(self.weights[0].cpu().item(),2),round(self.weights[1].cpu().item(),2))))
        else:
            if full_output == True:
                label = str(CUDAcpl2np(self.weights))
            else:
                label =str(self.parallel_shape)
            dot.edge('-0',id_str,color="blue",label = label)
        dot.format = 'png'
        return Image(dot.render(path))